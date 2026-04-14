from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response
import subprocess, os, tempfile, shutil, json, yaml
from django.conf import settings
from .base import K8sBaseMixin

class HelmManagementMixin(K8sBaseMixin):
    """
    Helm 应用管理逻辑 (List, Install, Upgrade 等)
    使用 Mixin 方式，聚合到主 ViewSet 中
    """

    @action(detail=False, methods=['get'])
    def helm_list_local_charts(self, request):
        """
        获取已上传到本地存储的 Chart 列表
        """
        base_dir = getattr(settings, 'MEDIA_ROOT', 
                           os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'media'))
        charts_dir = os.path.join(base_dir, 'helm_charts')
        if not os.path.exists(charts_dir):
            return Response([])
        
        files = [f for f in os.listdir(charts_dir) if f.endswith('.tgz')]
        # 返回去掉扩展名的文件名作为标识，或者全量
        return Response([{"id": f, "name": f} for f in files])

    @action(detail=True, methods=['get'])
    def helm_list(self, request, pk=None):
        """
        获取 Helm 释放列表
        """
        cluster = self.get_object()
        namespace = request.query_params.get('namespace', '')
        kubeconfig_path = self._get_temp_kubeconfig(cluster)

        try:
            cmd = ['helm', 'list', '--output', 'json', '--kubeconfig', kubeconfig_path]
            if namespace:
                cmd.extend(['-n', namespace])
            else:
                cmd.append('--all-namespaces')

            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                return Response({"error": f"Helm 列表获取失败: {result.stderr or result.stdout}"},
                                status=status.HTTP_400_BAD_REQUEST)

            data = json.loads(result.stdout)

            # 使用 K8s API 补充副本状态 (支持 Deployment, StatefulSet, DaemonSet)
            status_map = {}
            try:
                from ..utils.k8s_helper import get_k8s_client
                from kubernetes import client as k8s_lib

                k8s_api = get_k8s_client(cluster)
                apps_v1 = k8s_lib.AppsV1Api(k8s_api)

                # 聚合获取所有资源
                resources = []
                if namespace:
                    resources.extend(apps_v1.list_namespaced_deployment(namespace).items)
                    resources.extend(apps_v1.list_namespaced_stateful_set(namespace).items)
                    resources.extend(apps_v1.list_namespaced_daemon_set(namespace).items)
                else:
                    resources.extend(apps_v1.list_deployment_for_all_namespaces().items)
                    resources.extend(apps_v1.list_stateful_set_for_all_namespaces().items)
                    resources.extend(apps_v1.list_daemon_set_for_all_namespaces().items)

                for r in resources:
                    labels = r.metadata.labels or {}
                    # 识别 Helm 实例名称：多维度兼容
                    instance = (
                        labels.get('app.kubernetes.io/instance') or 
                        labels.get('release') or 
                        labels.get('helm.sh/release') or
                        labels.get('app') # 最后的垫底尝试
                    )
                    if not instance:
                        continue

                    # 将实例名统一为小写，处理某些 Chart 的不规范行为
                    key = (r.metadata.namespace, instance.lower())
                    s = status_map.get(key, {'ready': 0, 'total': 0, 'images': set()})
                    
                    # 副本计数逻辑 (兼容不同资源)
                    try:
                        if hasattr(r.status, 'ready_replicas'): # Deployment / StatefulSet
                            s['ready'] += (r.status.ready_replicas or 0)
                            s['total'] += (r.status.replicas or 0)
                        elif hasattr(r.status, 'number_ready'): # DaemonSet
                            s['ready'] += (r.status.number_ready or 0)
                            s['total'] += (r.status.desired_number_scheduled or 0)
                    except Exception:
                        pass
                    
                    if r.spec and r.spec.template and r.spec.template.spec and r.spec.template.spec.containers:
                        for c in r.spec.template.spec.containers:
                            if c.image:
                                s['images'].add(c.image.split('/')[-1])

                    status_map[key] = s
            except Exception as e:
                import traceback
                print(f"K8s API 辅助获取状态严重错误: {traceback.format_exc()}")

            # 填充结果
            for release in data:
                r_ns = release.get('namespace')
                r_name = release.get('name')
                r_chart_full = release.get('chart', '')

                release['cluster_id'] = cluster.id
                # 尝试精准匹配和忽略大小写匹配
                info = status_map.get((r_ns, r_name)) or status_map.get((r_ns, r_name.lower()))
                
                release['replicas_status'] = f"{info['ready']}/{info['total']}" if info else "0/0"
                release['deployed_images'] = list(info['images']) if info and 'images' in info else []

                # origin_mode 简单判定
                persistent_path = self._get_persistent_chart_path(r_chart_full)
                release['origin_mode'] = 'upload' if os.path.exists(persistent_path) else 'repo'

            return Response(data)
        except Exception as e:
            return Response({"error": f"执行异常: {str(e)}"}, status=status.HTTP_400_BAD_REQUEST)
        finally:
            if os.path.exists(kubeconfig_path):
                os.remove(kubeconfig_path)

    @action(detail=True, methods=['post'])
    def helm_install(self, request, pk=None):
        """
        发布/安装 Helm Chart
        """
        cluster = self.get_object()
        name = request.data.get('name')
        chart = request.data.get('chart')
        namespace = request.data.get('namespace', 'default')
        version = request.data.get('version')

        if not all([name, chart]):
            return Response({"error": "缺少 Release 名称或 Chart 路径"}, status=status.HTTP_400_BAD_REQUEST)

        kubeconfig_path = self._get_temp_kubeconfig(cluster)
        try:
            cmd = ['helm', 'install', name, chart, '-n', namespace, '--kubeconfig', kubeconfig_path]
            if version:
                cmd.extend(['--version', str(version)])
                
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                return Response({"error": f"安装失败: {result.stderr or result.stdout}"},
                                status=status.HTTP_400_BAD_REQUEST)

            return Response({"msg": f"Chart {name} 发布成功", "output": result.stdout})
        except Exception as e:
            return Response({"error": f"安装异常: {str(e)}"}, status=status.HTTP_400_BAD_REQUEST)
        finally:
            if os.path.exists(kubeconfig_path):
                os.remove(kubeconfig_path)

    @action(detail=True, methods=['post'])
    def helm_uninstall(self, request, pk=None):
        """
        卸载 Helm Release
        """
        cluster = self.get_object()
        name = request.data.get('name')
        namespace = request.data.get('namespace', 'default')

        if not name:
            return Response({"error": "缺少 Release 名称"}, status=status.HTTP_400_BAD_REQUEST)

        kubeconfig_path = self._get_temp_kubeconfig(cluster)
        try:
            cmd = ['helm', 'uninstall', name, '-n', namespace, '--kubeconfig', kubeconfig_path]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                return Response({"error": f"卸载失败: {result.stderr or result.stdout}"},
                                status=status.HTTP_400_BAD_REQUEST)

            return Response({"msg": f"Release {name} 卸载成功"})
        except Exception as e:
            return Response({"error": f"卸载异常: {str(e)}"}, status=status.HTTP_400_BAD_REQUEST)
        finally:
            if os.path.exists(kubeconfig_path):
                os.remove(kubeconfig_path)

    @action(detail=True, methods=['post'])
    def helm_upgrade(self, request, pk=None):
        """
        升级/修改 Helm Release 配置
        """
        cluster = self.get_object()
        name = request.data.get('name')
        chart = request.data.get('chart')
        namespace = request.data.get('namespace', 'default')
        force = request.data.get('force', False)
        values_content = request.data.get('values')

        if not name:
            return Response({"error": "缺少 Release 名称"}, status=status.HTTP_400_BAD_REQUEST)

        kubeconfig_path = self._get_temp_kubeconfig(cluster)
        temp_val_path = None

        try:
            if not chart:
                list_cmd = ['helm', 'list', '-n', namespace, '--filter', f'^{name}$', '--output', 'json', '--kubeconfig', kubeconfig_path]
                list_res = subprocess.run(list_cmd, capture_output=True, text=True)
                if list_res.returncode == 0:
                    releases = json.loads(list_res.stdout)
                    if releases:
                        c_name = releases[0].get('chart')
                        p_path = self._get_persistent_chart_path(c_name)
                        chart = p_path if os.path.exists(p_path) else c_name
            
            # --- 版本处理 ---
            version = request.data.get('version')

            if not chart:
                return Response({"error": "未能定位到该 Release 的 Chart 存储副本。"}, status=status.HTTP_400_BAD_REQUEST)

            cmd = ['helm', 'upgrade', name, chart, '-n', namespace, '--kubeconfig', kubeconfig_path, '--install']
            
            if version:
                cmd.extend(['--version', str(version)])

            if values_content:
                fd, temp_val_path = tempfile.mkstemp(suffix='.yaml')
                with os.fdopen(fd, 'w') as f:
                    f.write(values_content)
                cmd.extend(['-f', temp_val_path])

            if force: 
                cmd.extend(['--server-side=true', '--force-conflicts'])

            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                return Response({"error": f"升级失败: {result.stderr or result.stdout}"}, status=status.HTTP_400_BAD_REQUEST)

            return Response({"msg": f"Release {name} 升级/配置更新成功"})
        except Exception as e:
            return Response({"error": f"升级异常: {str(e)}"}, status=status.HTTP_400_BAD_REQUEST)
        finally:
            if os.path.exists(kubeconfig_path): os.remove(kubeconfig_path)
            if temp_val_path and os.path.exists(temp_val_path): os.remove(temp_val_path)

    @action(detail=True, methods=['post'])
    def helm_rollback(self, request, pk=None):
        """
        回滚 Helm Release
        """
        cluster = self.get_object()
        name = request.data.get('name')
        revision = request.data.get('revision')
        namespace = request.data.get('namespace', 'default')

        if not all([name, revision]):
            return Response({"error": "缺少 Release 名称或 版本号"}, status=status.HTTP_400_BAD_REQUEST)

        kubeconfig_path = self._get_temp_kubeconfig(cluster)
        try:
            cmd = ['helm', 'rollback', name, str(revision), '-n', namespace, '--kubeconfig', kubeconfig_path]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                return Response({"error": f"回滚失败: {result.stderr or result.stdout}"}, status=status.HTTP_400_BAD_REQUEST)

            return Response({"msg": f"Release {name} 已回滚至版本 {revision}"})
        except Exception as e:
            return Response({"error": f"回滚异常: {str(e)}"}, status=status.HTTP_400_BAD_REQUEST)
        finally:
            if os.path.exists(kubeconfig_path): os.remove(kubeconfig_path)

    @action(detail=True, methods=['get'])
    def helm_history(self, request, pk=None):
        """
        获取 Helm Release 历史版本
        """
        cluster = self.get_object()
        name = request.query_params.get('name')
        namespace = request.query_params.get('namespace', 'default')

        if not name:
            return Response({"error": "缺少 Release 名称"}, status=status.HTTP_400_BAD_REQUEST)

        kubeconfig_path = self._get_temp_kubeconfig(cluster)
        try:
            cmd = ['helm', 'history', name, '-n', namespace, '--output', 'json', '--kubeconfig', kubeconfig_path]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                return Response({"error": f"获取历史失败: {result.stderr or result.stdout}"}, status=status.HTTP_400_BAD_REQUEST)

            data = json.loads(result.stdout)
            return Response(data)
        except Exception as e:
            return Response({"error": f"执行异常: {str(e)}"}, status=status.HTTP_400_BAD_REQUEST)
        finally:
            if os.path.exists(kubeconfig_path): os.remove(kubeconfig_path)

    @action(detail=True, methods=['get'])
    def helm_get_values(self, request, pk=None):
        """
        获取 Helm Release 的 Values 配置
        """
        cluster = self.get_object()
        name = request.query_params.get('name')
        namespace = request.query_params.get('namespace', 'default')
        all_values = request.query_params.get('all', 'false').lower() == 'true'

        if not name:
            return Response({"error": "缺少 Release 名称"}, status=status.HTTP_400_BAD_REQUEST)

        kubeconfig_path = self._get_temp_kubeconfig(cluster)
        try:
            # -a 是显示所有配置，去掉可以只显示手动改动的，可以配合流水线kaniko_build生成的镜像名与版本号
            cmd = ['helm', 'get', 'values', name, '-n', namespace, '--output', 'json', '--kubeconfig', kubeconfig_path]
            if all_values: cmd.append('--all')

            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                return Response({"error": f"获取配置失败: {result.stderr or result.stdout}"}, status=status.HTTP_400_BAD_REQUEST)

            data = json.loads(result.stdout)
            yaml_str = yaml.dump(data, allow_unicode=True)
            return Response({"values": data, "yaml": yaml_str})
        except Exception as e:
            return Response({"error": f"执行异常: {str(e)}"}, status=status.HTTP_400_BAD_REQUEST)
        finally:
            if os.path.exists(kubeconfig_path): os.remove(kubeconfig_path)

    @action(detail=True, methods=['post'])
    def helm_restart(self, request, pk=None):
        """
        重启 Helm Release
        """
        cluster = self.get_object()
        name = request.data.get('name')
        namespace = request.data.get('namespace', 'default')

        if not name:
            return Response({"error": "缺少 Release 名称"}, status=status.HTTP_400_BAD_REQUEST)

        kubeconfig_path = self._get_temp_kubeconfig(cluster)
        try:
            cmd = ['kubectl', 'rollout', 'restart', 'deployment', '-n', namespace, '--kubeconfig', kubeconfig_path,
                   '--field-manager=helm', '-l', f'app.kubernetes.io/instance={name}']

            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                cmd2 = ['kubectl', 'rollout', 'restart', 'deployment', name, '-n', namespace, '--kubeconfig', kubeconfig_path, '--field-manager=helm']
                result = subprocess.run(cmd2, capture_output=True, text=True)
                if result.returncode != 0:
                    return Response({"error": f"重启失败: {result.stderr or result.stdout}"}, status=status.HTTP_400_BAD_REQUEST)

            return Response({"msg": f"Release {name} 关联服务已触发滚动重启"})
        except Exception as e:
            return Response({"error": f"重启异常: {str(e)}"}, status=status.HTTP_400_BAD_REQUEST)
        finally:
            if os.path.exists(kubeconfig_path): os.remove(kubeconfig_path)

    @action(detail=True, methods=['post'])
    def helm_stop(self, request, pk=None):
        """
        停止 Helm Release
        """
        cluster = self.get_object()
        name = request.data.get('name')
        namespace = request.data.get('namespace', 'default')

        if not name:
            return Response({"error": "缺少 Release 名称"}, status=status.HTTP_400_BAD_REQUEST)

        kubeconfig_path = self._get_temp_kubeconfig(cluster)
        try:
            cmd = ['kubectl', 'patch', 'deployment', '-n', namespace, '-p', '{"spec":{"replicas":0}}', '--kubeconfig', kubeconfig_path,
                   '--field-manager=helm', '-l', f'app.kubernetes.io/instance={name}']
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                cmd2 = ['kubectl', 'patch', 'deployment', name, '-n', namespace, '-p', '{"spec":{"replicas":0}}', '--kubeconfig', kubeconfig_path, '--field-manager=helm']
                result = subprocess.run(cmd2, capture_output=True, text=True)
                if result.returncode != 0:
                    return Response({"error": f"停止失败: {result.stderr or result.stdout}"}, status=status.HTTP_400_BAD_REQUEST)

            return Response({"msg": f"Release {name} 关联服务已停止 (副本设为 0)"})
        except Exception as e:
            return Response({"error": f"操作异常: {str(e)}"}, status=status.HTTP_400_BAD_REQUEST)
        finally:
            if os.path.exists(kubeconfig_path): os.remove(kubeconfig_path)

    @action(detail=True, methods=['post'])
    def chart_upload(self, request, pk=None):
        """
        上传本地 Chart 并发布
        """
        cluster = self.get_object()
        name = request.data.get('name')
        namespace = request.data.get('namespace', 'default')
        file_obj = request.FILES.get('file')
        is_upgrade = request.data.get('is_upgrade') == 'true' or request.data.get('is_upgrade') is True
        force = request.data.get('force') == 'true' or request.data.get('force') is True
        values_content = request.data.get('values')

        if not name: return Response({"error": "缺少 Release 名称"}, status=status.HTTP_400_BAD_REQUEST)

        temp_dir = tempfile.mkdtemp()
        kubeconfig_path = self._get_temp_kubeconfig(cluster)
        chart_path = None

        try:
            if file_obj:
                chart_path = os.path.join(temp_dir, file_obj.name)
                with open(chart_path, 'wb+') as destination:
                    for chunk in file_obj.chunks(): destination.write(chunk)
            elif is_upgrade:
                list_cmd = ['helm', 'list', '-n', namespace, '--filter', f'^{name}$', '--output', 'json', '--kubeconfig', kubeconfig_path]
                list_res = subprocess.run(list_cmd, capture_output=True, text=True)
                if list_res.returncode == 0:
                    releases = json.loads(list_res.stdout)
                    if releases:
                        c_fullname = releases[0].get('chart')
                        p_path = self._get_persistent_chart_path(c_fullname)
                        if os.path.exists(p_path): chart_path = p_path

            if not chart_path: return Response({"error": "缺少 Chart 文件。"}, status=status.HTTP_400_BAD_REQUEST)

            cmd_args = []
            if values_content:
                v_fd, temp_val_path = tempfile.mkstemp(suffix='.yaml', dir=temp_dir)
                with os.fdopen(v_fd, 'w') as f: f.write(values_content)
                cmd_args.extend(['-f', temp_val_path])

            op = 'upgrade' if is_upgrade else 'install'
            cmd = ['helm', op, name, chart_path, '-n', namespace, '--kubeconfig', kubeconfig_path]
            cmd.extend(cmd_args)
            
            if is_upgrade and force:
                cmd.extend(['--server-side=true', '--force-conflicts'])

            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                return Response({"error": f"{'升级' if is_upgrade else '发布'}失败: {result.stderr or result.stdout}"}, status=status.HTTP_400_BAD_REQUEST)

            if file_obj:
                try:
                    list_cmd = ['helm', 'list', '-n', namespace, '--filter', f'^{name}$', '--output', 'json', '--kubeconfig', kubeconfig_path]
                    list_res = subprocess.run(list_cmd, capture_output=True, text=True)
                    if list_res.returncode == 0:
                        releases = json.loads(list_res.stdout)
                        if releases:
                            c_fullname = releases[0].get('chart')
                            persistent_path = self._get_persistent_chart_path(c_fullname)
                            shutil.copy2(chart_path, persistent_path)
                except Exception: pass

            return Response({"msg": f"本地 Chart {name} 操作成功"})
        except Exception as e:
            return Response({"error": f"操作异常: {str(e)}"}, status=status.HTTP_400_BAD_REQUEST)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
            if os.path.exists(kubeconfig_path): os.remove(kubeconfig_path)

    @action(detail=True, methods=['post'])
    def chart_scaffold(self, request, pk=None):
        """
        生成脚手架文件
        """
        name = request.data.get('name', 'my-chart')
        temp_dir = tempfile.mkdtemp()
        try:
            proc = subprocess.run(['helm', 'create', name], cwd=temp_dir, capture_output=True, text=True)
            if proc.returncode != 0: return Response({"error": f"脚手架生成失败: {proc.stderr}"}, status=status.HTTP_400_BAD_REQUEST)

            scaffold_path = os.path.join(temp_dir, name)
            files = []
            for root, dirs, filenames in os.walk(scaffold_path):
                for f in filenames:
                    abs_path = os.path.join(root, f)
                    rel_path = os.path.relpath(abs_path, scaffold_path)
                    try:
                        with open(abs_path, 'r', encoding='utf-8') as content_file:
                            files.append({'path': rel_path, 'content': content_file.read()})
                    except Exception: pass
            return Response(files)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    @action(detail=True, methods=['post'])
    def chart_create(self, request, pk=None):
        """
        在线创建并发布基础 Chart
        """
        cluster = self.get_object()
        name = request.data.get('name')
        namespace = request.data.get('namespace', 'default')
        custom_files = request.data.get('files')

        if not name: return Response({"error": "缺少 Release 名称"}, status=status.HTTP_400_BAD_REQUEST)

        temp_dir = tempfile.mkdtemp()
        kubeconfig_path = self._get_temp_kubeconfig(cluster)
        try:
            chart_path = os.path.join(temp_dir, name)
            if custom_files and isinstance(custom_files, list):
                os.makedirs(chart_path, exist_ok=True)
                for f_data in custom_files:
                    f_rel_path = f_data.get('path')
                    f_content = f_data.get('content', '')
                    if not f_rel_path: continue
                    full_p = os.path.join(chart_path, f_rel_path)
                    os.makedirs(os.path.dirname(full_p), exist_ok=True)
                    with open(full_p, 'w', encoding='utf-8') as f: f.write(f_content)
            else:
                subprocess.run(['helm', 'create', name], cwd=temp_dir, capture_output=True, check=True)

            cmd = ['helm', 'install', name, chart_path, '-n', namespace, '--kubeconfig', kubeconfig_path]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                return Response({"error": f"创建并发布失败: {result.stderr or result.stdout}"}, status=status.HTTP_400_BAD_REQUEST)

            # 打包并保存以供后续流水线复用该 Chart
            try:
                subprocess.run(['helm', 'package', chart_path, '-d', temp_dir], capture_output=True, check=True)
                list_cmd = ['helm', 'list', '-n', namespace, '--filter', f'^{name}$', '--output', 'json', '--kubeconfig', kubeconfig_path]
                list_res = subprocess.run(list_cmd, capture_output=True, text=True)
                if list_res.returncode == 0:
                    releases = json.loads(list_res.stdout)
                    if releases:
                        c_fullname = releases[0].get('chart')
                        persistent_path = self._get_persistent_chart_path(c_fullname)
                        for f in os.listdir(temp_dir):
                            if f.endswith('.tgz'):
                                shutil.copy2(os.path.join(temp_dir, f), persistent_path)
                                break
            except Exception: pass

            return Response({"msg": f"Chart {name} 发布成功"})
        except Exception as e:
            return Response({"error": f"操作异常: {str(e)}"}, status=status.HTTP_400_BAD_REQUEST)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
            if os.path.exists(kubeconfig_path): os.remove(kubeconfig_path)