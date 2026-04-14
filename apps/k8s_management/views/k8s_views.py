from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response
from kubernetes import client as k8s_client
import datetime, yaml
from kubernetes.stream import stream
from .base import K8sBaseMixin
from ..utils.k8s_helper import get_k8s_client

class K8sManagementMixin(K8sBaseMixin):
    """
    k8s 基础资源管理逻辑 (Nodes, Pods, Deployments 等)
    使用 Mixin 方式，聚合到主 ViewSet 中
    """

    @action(detail=True, methods=['post'])
    def verify(self, request, pk=None):
        """
        实时验证 K8s 连通性
        """
        cluster = self.get_object()
        try:
            api_instance = k8s_client.VersionApi(get_k8s_client(cluster))
            version_info = api_instance.get_code()

            cluster.status = 'connected'
            cluster.version = version_info.git_version
            cluster.save()

            return Response({
                "msg": "连接成功",
                "version": version_info.git_version
            })
        except Exception as e:
            cluster.status = 'failed'
            cluster.save()
            return Response({"error": f"连接失败: {str(e)}"}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['get'])
    def nodes_list(self, request, pk=None):
        """
        获取集群节点列表
        """
        cluster = self.get_object()
        try:
            api_instance = k8s_client.CoreV1Api(get_k8s_client(cluster))
            nodes = api_instance.list_node()
            data = []
            for node in nodes.items:
                data.append({
                    "name": node.metadata.name,
                    "status": "Ready" if any(cond.type == 'Ready' and cond.status == 'True' for cond in
                                             node.status.conditions) else "NotReady",
                    "roles": [label.split('/')[-1] for label in node.metadata.labels if
                               'node-role.kubernetes.io/' in label],
                    "version": node.status.node_info.kubelet_version,
                    "internal_ip": next((addr.address for addr in node.status.addresses if addr.type == 'InternalIP'),
                                        None),
                    "cpu": node.status.capacity.get('cpu'),
                    "memory": node.status.capacity.get('memory'),
                    "creation_timestamp": node.metadata.creation_timestamp
                })
            return Response(data)
        except Exception as e:
            return Response({"error": f"获取节点失败: {str(e)}"}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['get'])
    def namespaces_list(self, request, pk=None):
        """
        获取命名空间列表
        """
        cluster = self.get_object()
        try:
            api_instance = k8s_client.CoreV1Api(get_k8s_client(cluster))
            namespaces = api_instance.list_namespace()
            data = [ns.metadata.name for ns in namespaces.items]
            return Response(data)
        except Exception as e:
            return Response({"error": f"获取命名空间失败: {str(e)}"}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['get'])
    def pods_list(self, request, pk=None):
        """
        获取 Pod 列表
        """
        cluster = self.get_object()
        namespace = request.query_params.get('namespace', None)
        try:
            api_instance = k8s_client.CoreV1Api(get_k8s_client(cluster))
            if namespace:
                pods = api_instance.list_namespaced_pod(namespace)
            else:
                pods = api_instance.list_pod_for_all_namespaces()
            data = []
            for pod in pods.items:
                data.append({
                    "name": pod.metadata.name,
                    "namespace": pod.metadata.namespace,
                    "status": pod.status.phase,
                    "pod_ip": pod.status.pod_ip,
                    "node_name": pod.spec.node_name,
                    "creation_timestamp": pod.metadata.creation_timestamp,
                    "restarts": sum(cs.restart_count for cs in
                                    pod.status.container_statuses) if pod.status.container_statuses else 0
                })
            return Response(data)
        except Exception as e:
            return Response({"error": f"获取 Pod 失败: {str(e)}"}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['get'])
    def deployments_list(self, request, pk=None):
        """
        获取 Deployment 列表
        """
        cluster = self.get_object()
        namespace = request.query_params.get('namespace', None)
        try:
            api_instance = k8s_client.AppsV1Api(get_k8s_client(cluster))
            if namespace:
                deployments = api_instance.list_namespaced_deployment(namespace)
            else:
                deployments = api_instance.list_deployment_for_all_namespaces()
            data = []
            for dep in deployments.items:
                data.append({
                    "name": dep.metadata.name,
                    "namespace": dep.metadata.namespace,
                    "replicas": f"{dep.status.available_replicas or 0}/{dep.spec.replicas}",
                    "strategy": dep.spec.strategy.type,
                    "creation_timestamp": dep.metadata.creation_timestamp
                })
            return Response(data)
        except Exception as e:
            return Response({"error": f"获取 Deployment 失败: {str(e)}"}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['get'])
    def services_list(self, request, pk=None):
        """
        获取 Service 列表
        """
        cluster = self.get_object()
        namespace = request.query_params.get('namespace', None)
        try:
            api_instance = k8s_client.CoreV1Api(get_k8s_client(cluster))
            if namespace:
                services = api_instance.list_namespaced_service(namespace)
            else:
                services = api_instance.list_service_for_all_namespaces()
            data = []
            for svc in services.items:
                data.append({
                    "name": svc.metadata.name,
                    "namespace": svc.metadata.namespace,
                    "type": svc.spec.type,
                    "cluster_ip": svc.spec.cluster_ip,
                    "ports": [{"port": p.port, "protocol": p.protocol, "node_port": p.node_port} for p in
                               svc.spec.ports],
                    "creation_timestamp": svc.metadata.creation_timestamp
                })
            return Response(data)
        except Exception as e:
            return Response({"error": f"获取 Service 失败: {str(e)}"}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['get'])
    def pod_logs_list(self, request, pk=None):
        """
        获取 Pod 日志 (增强型：支持在未就绪时自动回退到事件日志或 Init 容器日志)
        """
        cluster = self.get_object()
        namespace = request.query_params.get('namespace')
        pod_name = request.query_params.get('pod_name')
        container = request.query_params.get('container')
        tail_lines = int(request.query_params.get('tail_lines', 500))

        if not namespace or not pod_name:
            return Response({"error": "缺少命名空间或 Pod 名称"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            client_api = get_k8s_client(cluster)
            api_instance = k8s_client.CoreV1Api(client_api)
            
            # 1. 尝试直接获取主容器日志
            try:
                logs = api_instance.read_namespaced_pod_log(
                    name=pod_name,
                    namespace=namespace,
                    container=container,
                    tail_lines=tail_lines
                )
                return Response({"logs": logs})
            except Exception as e:
                error_msg = str(e)
                # 如果是容器正在初始化或等待启动 (API 返回 400)
                if "BadRequest" in error_msg or "waiting to start" in error_msg:
                    # 2. 尝试获取 Pod 当前状态和事件 (类似 kubectl describe)
                    pod = api_instance.read_namespaced_pod(pod_name, namespace)
                    
                    # 2a. 检查是否有 Init 容器正在运行或报错，尝试提取其日志
                    if pod.spec.init_containers:
                        for ic in pod.spec.init_containers:
                            try:
                                i_logs = api_instance.read_namespaced_pod_log(
                                    name=pod_name,
                                    namespace=namespace,
                                    container=ic.name,
                                    tail_lines=tail_lines
                                )
                                if i_logs:
                                    prefix = f"[Init Container: {ic.name} Logs]\n" + "="*40 + "\n"
                                    return Response({"logs": prefix + i_logs})
                            except:
                                continue

                    # 2b. 如果没有运行的 Init 容器日志，拉取 K8s 事件记录
                    events = api_instance.list_namespaced_event(
                        namespace, 
                        field_selector=f"involvedObject.name={pod_name},involvedObject.kind=Pod"
                    )
                    
                    diagnostic_info = [
                        f"⚠️ 容器尚未就绪，无法直接获取日志。当前 Pod 状态: {pod.status.phase}",
                        f"提示: {error_msg.split('Reason:')[0].strip()}",
                        "\n== 最近事件 (Pod Events) =="
                    ]
                    
                    sorted_events = sorted(events.items, key=lambda x: x.last_timestamp or x.first_timestamp or datetime.datetime.now(), reverse=True)
                    for ev in sorted_events[:10]:
                        ts = ev.last_timestamp or ev.first_timestamp
                        ts_str = ts.strftime('%H:%M:%S') if ts else '??:??'
                        diagnostic_info.append(f"[{ts_str}] {ev.reason}: {ev.message}")

                    if not events.items:
                        diagnostic_info.append("(暂无相关事件)")
                        
                    return Response({
                        "logs": "\n".join(diagnostic_info),
                        "is_diagnostic": True
                    })
                
                # 其他类型的错误（如权限、网络）则正常抛出
                raise e
                
        except Exception as e:
            return Response({"error": f"获取日志失败: {str(e)}"}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'])
    def scale_deployment(self, request, pk=None):
        """
        自动扩缩容 Deployment
        """
        cluster = self.get_object()
        namespace = request.data.get('namespace')
        name = request.data.get('name')
        replicas = request.data.get('replicas')

        if not all([namespace, name, replicas is not None]):
            return Response({"error": "参数不完整"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            api_instance = k8s_client.AppsV1Api(get_k8s_client(cluster))
            body = {"spec": {"replicas": int(replicas)}}
            # 改用水准的 patch_namespaced_deployment 而不是 patch_namespaced_deployment_scale
            # 这样 K8s 就不会把所有权记录在 "scale" subresource 上，导致 Helm 即便用了 --force-conflicts 也无法跨 subresource 抢夺所有权。
            api_instance.patch_namespaced_deployment(
                name, namespace, body, field_manager="helm"
            )
            return Response({"msg": f"已将 {name} 调整为 {replicas} 个副本"})
        except Exception as e:
            return Response({"error": f"扩缩容失败: {str(e)}"}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'])
    def restart_deployment(self, request, pk=None):
        """
        滚动重启 Deployment
        """
        cluster = self.get_object()
        namespace = request.data.get('namespace')
        name = request.data.get('name')

        if not namespace or not name:
            return Response({"error": "缺少必要参数"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            api_instance = k8s_client.AppsV1Api(get_k8s_client(cluster))
            # 通过更新注解触发滚动重启
            now = datetime.datetime.now().isoformat()
            body = {
                'spec': {
                    'template': {
                        'metadata': {
                            'annotations': {
                                'kubectl.kubernetes.io/restartedAt': now
                            }
                        }
                    }
                }
            }
            api_instance.patch_namespaced_deployment(
                name, namespace, body, field_manager="helm"
            )
            return Response({"msg": f"已触发 {name} 的滚动重启"})
        except Exception as e:
            return Response({"error": f"重启失败: {str(e)}"}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['get'])
    def yaml_list(self, request, pk=None):
        """
        获取资源的 YAML 定义
        """
        cluster = self.get_object()
        res_type = request.query_params.get('type')
        name = request.query_params.get('name')
        namespace = request.query_params.get('namespace')

        if not all([res_type, name]):
            return Response({"error": "缺少资源类型或名称"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            api_client = get_k8s_client(cluster)
            core_api = k8s_client.CoreV1Api(api_client)
            apps_api = k8s_client.AppsV1Api(api_client)

            if res_type == 'pod':
                resp = core_api.read_namespaced_pod(name, namespace)
            elif res_type == 'deployment':
                resp = apps_api.read_namespaced_deployment(name, namespace)
            elif res_type == 'service':
                resp = core_api.read_namespaced_service(name, namespace)
            elif res_type == 'node':
                resp = core_api.read_node(name)
            else:
                return Response({"error": "不支持的资源类型"}, status=status.HTTP_400_BAD_REQUEST)

            # 将对象转换为字典并转为 YAML 字符串
            data = api_client.sanitize_for_serialization(resp)
            yaml_str = yaml.dump(data, default_flow_style=False)
            return Response({"yaml": yaml_str})
        except Exception as e:
            return Response({"error": f"获取 YAML 失败: {str(e)}"}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'])
    def update_yaml(self, request, pk=None):
        """
        更新资源的 YAML 定义
        """
        cluster = self.get_object()
        yaml_content = request.data.get('yaml')
        if not yaml_content:
            return Response({"error": "YAML 内容不能为空"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            api_client = get_k8s_client(cluster)
            # 解析 YAML
            yaml_obj = yaml.safe_load(yaml_content)

            # 如果是更新，根据 metadata 里的信息来判断
            kind = yaml_obj.get('kind')
            name = yaml_obj.get('metadata', {}).get('name')
            namespace = yaml_obj.get('metadata', {}).get('namespace')

            if kind == 'Deployment':
                k8s_client.AppsV1Api(api_client).patch_namespaced_deployment(
                    name, namespace, yaml_obj, field_manager="helm"
                )
            elif kind == 'Service':
                k8s_client.CoreV1Api(api_client).patch_namespaced_service(
                    name, namespace, yaml_obj, field_manager="helm"
                )
            elif kind == 'Pod':
                k8s_client.CoreV1Api(api_client).patch_namespaced_pod(
                    name, namespace, yaml_obj, field_manager="helm"
                )
            else:
                return Response({"error": f"暂不支持更新类型: {kind}"}, status=status.HTTP_400_BAD_REQUEST)

            return Response({"msg": "更新成功"})
        except Exception as e:
            return Response({"error": f"更新失败: {str(e)}"}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'])
    def pod_exec(self, request, pk=None):
        """
        在 Pod 容器中执行命令（非交互式）
        """
        cluster = self.get_object()
        data = request.data
        namespace = data.get('namespace')
        pod_name = data.get('pod_name') or data.get('name')
        container = data.get('container')
        command = data.get('command')

        if not all([namespace, pod_name, command]):
            return Response({
                "error": f"缺少必要参数: namespace={namespace}, pod_name={pod_name}, command={command}"
            }, status=status.HTTP_400_BAD_REQUEST)

        try:
            api_client = get_k8s_client(cluster)
            core_api = k8s_client.CoreV1Api(api_client)

            # 自动识别容器
            if not container:
                pod_obj = core_api.read_namespaced_pod(pod_name, namespace)
                if pod_obj.spec.containers:
                    container = pod_obj.spec.containers[0].name

            if isinstance(command, str):
                command = ["/bin/sh", "-c", command]

            resp = stream(core_api.connect_get_namespaced_pod_exec,
                          pod_name,
                          namespace,
                          container=container,
                          command=command,
                          stderr=True, stdin=False,
                          stdout=True, tty=False)

            return Response({"output": resp})
        except Exception as e:
            msg = str(e)
            if "Forbidden" in msg:
                msg = ("K8s 权限不足 (403 Forbidden)。\n"
                       "请执行: kubectl create clusterrolebinding ansflow-admin-fix "
                       "--clusterrole=cluster-admin --serviceaccount=default:default")
            return Response({"error": f"执行失败: {msg}"}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'])
    def delete_pod(self, request, pk=None):
        """
        删除 Pod 资源
        """
        cluster = self.get_object()
        namespace = request.data.get('namespace')
        name = request.data.get('name')

        if not namespace or not name:
            return Response({"error": "缺少命名空间或 Pod 名称"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            api_instance = k8s_client.CoreV1Api(get_k8s_client(cluster))
            api_instance.delete_namespaced_pod(name, namespace)
            return Response({"msg": f"Pod {name} 正在删除..."})
        except Exception as e:
            return Response({"error": f"删除失败: {str(e)}"}, status=status.HTTP_400_BAD_REQUEST)