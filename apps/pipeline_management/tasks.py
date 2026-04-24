import time
import os
import logging
import requests
from celery import shared_task
from django.utils import timezone
from apps.pipeline_management.models import Pipeline, PipelineRun, PipelineNodeRun
from celery.exceptions import SoftTimeLimitExceeded
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

logger = logging.getLogger(__name__)

def push_pipeline_status_to_ws(run_obj):
    """
    通过 WebSocket 实时推送流水线及其所有节点的最新状态
    """
    channel_layer = get_channel_layer()
    async_to_sync(channel_layer.group_send)(
        f"pipeline_run_{run_obj.id}",
        {
            "type": "pipeline_run_update",
            "data": {
                "id": run_obj.id,
                "status": run_obj.status,
                "pipeline_name": run_obj.pipeline.name,
                "trigger_user_name": run_obj.trigger_user.username if run_obj.trigger_user else '系统',
                "start_time": run_obj.start_time.isoformat() if run_obj.start_time else None,
                "end_time": run_obj.end_time.isoformat() if run_obj.end_time else None,
                "nodes": [
                    {
                        "node_id": n.node_id,
                        "status": n.status,
                        "logs": n.logs or "",
                        "start_time": n.start_time.isoformat() if n.start_time else None,
                        "end_time": n.end_time.isoformat() if n.end_time else None,
                        "output_data": n.output_data
                    } for n in run_obj.nodes.all()
                ]
            }
        }
    )
    
    # 推送给全局列表监听组 (所有人的列表页、历史页等)
    async_to_sync(channel_layer.group_send)(
        "pipeline_all",
        {
            "type": "pipeline_all_update",
            "data": {
                "id": run_obj.id,
                "status": run_obj.status,
                "pipeline_id": run_obj.pipeline.id,
                "pipeline_name": run_obj.pipeline.name,
                "trigger_user_name": run_obj.trigger_user.username if run_obj.trigger_user else '系统',
                "start_time": run_obj.start_time.isoformat() if run_obj.start_time else None,
                "end_time": run_obj.end_time.isoformat() if run_obj.end_time else None,
            }
        }
    )

@shared_task(bind=True)
def execute_pipeline_node(self, node_run_id):
    """
    具体的单个节点执行器，执行完后不管成败，均回调引擎继续决策调度
    """
    node_run = PipelineNodeRun.objects.get(id=node_run_id)
    
    # --- 增加：如果流水线已被取消，则直接退出 ---
    if node_run.run.status == 'cancelled':
        node_run.status = 'failed'
        node_run.logs = "流水线已取消，停止执行本节点。"
        node_run.save()
        return

    node_run.status = 'running'
    node_run.start_time = timezone.now()
    node_run.celery_task_id = self.request.id
    node_run.save()
    
    # 实时推送：进入运行状态
    push_pipeline_status_to_ws(node_run.run)

    run_id = node_run.run_id
    success = False
    
    import os
    import subprocess
    import shutil
    
    # 统一工作区路径：基于 PipelineRun 的 ID，所有容器和脚本挂载都在此进行
    # 重试时复用父 run 的工作目录，避免重新 clone 代码
    parent_run_id = node_run.run.parent_run_id
    if parent_run_id:
        workspace_dir = f"/tmp/ansflow_workspaces/run_{parent_run_id}"
    else:
        workspace_dir = f"/tmp/ansflow_workspaces/run_{run_id}"
    os.makedirs(workspace_dir, exist_ok=True)
    source_dir = os.path.join(workspace_dir, 'source')
    
    try:
        # ---- 根据 node_type 进行不同业务分流 ----
        node_type = node_run.node_type
        
        if node_type == 'input':
            node_run.logs = "起点触发完成。"
            success = True
            
        elif node_type == 'git_clone':
            pipeline_graph = node_run.run.pipeline.graph_data
            nodes_config = pipeline_graph.get('nodes', [])
            current_node_config = next((n for n in nodes_config if n.get('id') == node_run.node_id), {})
            node_data = current_node_config.get('data', {})
            
            repo_url = node_data.get('git_repo')
            branch = node_data.get('git_branch', 'main')
            
            if not repo_url:
                raise ValueError("Git 节点未配置仓库地址(URL)")
                
            node_run.logs = f"准备克隆拉取代码: {repo_url} (分支: {branch})...\n工作区挂载: {source_dir}\n"
            node_run.save()
            
            # 保证拉取前目录干净
            if os.path.exists(source_dir):
                shutil.rmtree(source_dir, ignore_errors=True)
                
            try:
                cmd = ["git", "clone", "-b", branch, repo_url, source_dir]
                result = subprocess.run(cmd, capture_output=True, text=True, check=True)
                node_run.logs += result.stdout + "\n代码拉取成功！已放入统一工作区。"
                success = True
            except subprocess.CalledProcessError as e:
                node_run.logs += f"代码拉取失败:\n{e.stderr}"
                success = False
                
        elif node_type == 'docker_build':
            pipeline_graph = node_run.run.pipeline.graph_data
            nodes_config = pipeline_graph.get('nodes', [])
            current_node_config = next((n for n in nodes_config if n.get('id') == node_run.node_id), {})
            node_data = current_node_config.get('data', {})
            
            ci_env_id = node_data.get('ci_env_id')
            build_script = node_data.get('build_script')
            
            if not ci_env_id or not build_script:
                raise ValueError("编译沙箱节点缺少环境或编译指令配置")
                
            from apps.pipeline_management.models import CIEnvironment
            try:
                env_obj = CIEnvironment.objects.get(id=ci_env_id)
            except CIEnvironment.DoesNotExist:
                raise ValueError(f"指定的执行沙箱环境(ID:{ci_env_id})不存在或已被删除")
                
            image_name = env_obj.image
            
            node_run.logs = f"正在启动 Docker 容器沙箱编译...\n> 工作区映射: {source_dir} -> /workspace\n> 拉起底层镜像: {image_name}\n> 注入的构建指令:\n{build_script}\n"
            node_run.save()
            
            if not os.path.exists(source_dir):
                raise ValueError("代码工作区为空，请检查本节点上方是否正确连接了 Git 拉取节点！")
                
            try:
                # --rm: 用完即毁, -v: 挂载代码, -w: 切换工作目录
                cmd = [
                    "docker", "run", "--rm",
                    "-v", f"{source_dir}:/workspace",
                    "-w", "/workspace",
                    image_name,
                    "/bin/sh", "-c", build_script
                ]
                
                logger.info(f"[DEBUG] docker_build 开始执行 node={node_run.node_id} workspace={workspace_dir} source_dir={source_dir}")
                logger.info(f"[DEBUG] cmd = {' '.join(cmd)}")

                node_run.logs += f"\n[$] {' '.join(cmd)}\n"

                # 避免 capture_output=True 死锁（maven 输出量大时 PIPE 缓冲会满）
                # 改用文件中转：实时写入文件，结束后读回日志
                import tempfile
                with tempfile.NamedTemporaryFile(mode='w+', suffix='.log', delete=False) as tmp:
                    tmp_path = tmp.name

                logger.info(f"[DEBUG] subprocess 开始，等待完成... tmp_path={tmp_path}")
                with open(tmp_path, 'w', buffering=1) as stdout_f:
                    result = subprocess.run(cmd, stdout=stdout_f, stderr=subprocess.STDOUT)
                logger.info(f"[DEBUG] subprocess 完成，returncode={result.returncode}")

                with open(tmp_path, 'r') as f:
                    node_run.logs += "\n--- 容器内的输出日志 ---\n"
                    node_run.logs += f.read()

                import os
                os.unlink(tmp_path)
                logger.info(f"[DEBUG] docker_build 完成，returncode={result.returncode}")

                if result.returncode == 0:
                    node_run.logs += "\n✨ 隔离沙箱编译执行成功！所有编译产物均已落回宿主机的工作区中。"
                    success = True
                else:
                    node_run.logs += f"\n❌ 沙箱编译失败，容器退出异常状态码: {result.returncode}"
                    success = False
            except Exception as e:
                node_run.logs += f"\n调用宿主机 Docker Daemon 失败: {str(e)}。请检查服务器是否安装并启动了 Docker。"
                success = False

        elif node_type == 'kaniko_build':
            pipeline_graph = node_run.run.pipeline.graph_data
            nodes_config = pipeline_graph.get('nodes', [])
            current_node_config = next((n for n in nodes_config if n.get('id') == node_run.node_id), {})
            node_data = current_node_config.get('data', {})
            
            registry_id = node_data.get('registry_id')
            image_name = node_data.get('image_name')
            image_tag = node_data.get('image_tag', f"v{run_id}")
            context_path = node_data.get('context_path', '.')
            dockerfile_path = node_data.get('dockerfile_path', 'Dockerfile')
            
            if not registry_id or not image_name:
                raise ValueError("Kaniko构建节点缺少 registry_id 或 image_name 配置")
                
            from apps.registry_management.models import ImageRegistry
            import json
            import base64
            
            try:
                registry = ImageRegistry.objects.get(id=registry_id)
            except ImageRegistry.DoesNotExist:
                raise ValueError(f"指定的镜像仓库(ID:{registry_id})不存在")
            
            registry_url_clean = registry.url.replace("https://", "").replace("http://", "").strip("/")
            is_docker_hub = "docker.io" in registry_url_clean or "hub.docker.com" in registry_url_clean
            
            if is_docker_hub:
                auth_url = "https://index.docker.io/v1/"
                push_host = "docker.io"
            else:
                auth_url = registry_url_clean
                push_host = registry_url_clean

            # 格式化镜像名称与 Tag，防止出现双冒号或非法标签
            # 如果 image_name 中已经包含了标签 (例如 my-app:v1)，则优先处理
            if ":" in image_name:
                parts = image_name.split(":", 1)
                real_name = parts[0]
                # 如果在界面上也填了 tag，或者有默认 tag，需要进行抉择
                # 这里我们采取策略：如果 image_name 带了 tag，且 tag 字段也是有效的，则可能发生了误填，我们尝试修复
                if image_tag and image_tag != f"v{run_id}":
                    # 此时 image_name="a:b", image_tag="c" -> 使用 image_tag 作为最终版本
                    image_name = real_name
                else:
                    # 如果 tag 字段是默认的，就用 name 里的 tag
                    image_name = real_name
                    image_tag = parts[1]

            # 清理可能存在的首尾冒号
            image_name = image_name.strip(":")
            image_tag = image_tag.strip(":")

            if registry.namespace:
                full_image = f"{push_host}/{registry.namespace}/{image_name}:{image_tag}"
            else:
                full_image = f"{push_host}/{image_name}:{image_tag}"
                
            node_run.logs = f"正在启动 Kaniko 构建...\n> 挂载代码: {source_dir}\n> 目标镜像: {full_image}\n"
            node_run.save()
            
            auth_string = f"{registry.username}:{registry.password}"
            auth_b64 = base64.b64encode(auth_string.encode('utf-8')).decode('utf-8')
            kaniko_dir = os.path.join(workspace_dir, '.kaniko')
            os.makedirs(kaniko_dir, exist_ok=True)
            config_json_path = os.path.join(kaniko_dir, 'config.json')
            
            auth_config = {
                "auths": {
                    auth_url: {
                        "auth": auth_b64
                    }
                }
            }
            with open(config_json_path, 'w') as f:
                json.dump(auth_config, f)
                
            try:
                cmd = [
                    "docker", "run", "--rm",
                    "-v", f"{source_dir}:/workspace",
                    "-v", f"{config_json_path}:/kaniko/.docker/config.json",
                    "gcr.io/kaniko-project/executor:debug",
                    "--context", f"dir:///workspace/{context_path}",
                    "--dockerfile", f"/workspace/{dockerfile_path}",
                    "--destination", full_image
                ]

                node_run.logs += f"\n[$] {' '.join(cmd)}\n"

                import tempfile
                with tempfile.NamedTemporaryFile(mode='w+', suffix='.log', delete=False) as tmp:
                    tmp_path = tmp.name

                with open(tmp_path, 'w', buffering=1) as stdout_f:
                    result = subprocess.run(cmd, stdout=stdout_f, stderr=subprocess.STDOUT)

                with open(tmp_path, 'r') as f:
                    node_run.logs += "\n--- Kaniko 构建输出 ---\n"
                    node_run.logs += f.read()

                import os
                os.unlink(tmp_path)

                if result.stderr:
                    node_run.logs += "\n--- Kaniko 标准异常 ---\n"
                    node_run.logs += result.stderr
                    
                if result.returncode == 0:
                    node_run.logs += f"\n Kaniko 镜像构建成功！并已推送到: {full_image}"
                    node_run.output_data = {
                        "repository": full_image.split(':')[0],
                        "tag": image_tag,
                        "full_image": full_image
                    }
                    success = True

                    # 自动记录产物到 Artifact 表
                    try:
                        from apps.registry_management.models import Artifact, ArtifactVersion
                        artifact_name = image_name.split('/')[-1] if '/' in image_name else image_name
                        artifact, created = Artifact.objects.get_or_create(
                            name=artifact_name,
                            image_registry=registry,
                            defaults={
                                'source_type': 'docker',
                                'type': 'docker_image',
                                'repository': image_name,
                                'latest_tag': image_tag,
                                'pipeline': node_run.run.pipeline,
                            }
                        )
                        if not created:
                            artifact.latest_tag = image_tag
                            artifact.repository = image_name
                            artifact.save(update_fields=['latest_tag', 'repository', 'update_time'])

                        # 创建版本记录
                        ArtifactVersion.objects.create(
                            artifact=artifact,
                            tag=image_tag,
                            pipeline_run=node_run.run,
                            build_user=node_run.run.trigger_user.username if node_run.run.trigger_user else None,
                        )
                        node_run.logs += f"\n[产物记录] 已创建/更新 Artifact: {artifact.name}:{image_tag}"
                    except Exception as art_err:
                        node_run.logs += f"\n[产物记录] 记录失败: {str(art_err)}"
                else:
                    node_run.logs += f"\n Kaniko 编译失败，退出码: {result.returncode}"
                    success = False
            except Exception as e:
                node_run.logs += f"\n执行 Kaniko 失败: {str(e)}"
                success = False
            
        elif node_type == 'ansible':
            # 获取配置
            pipeline_graph = node_run.run.pipeline.graph_data
            nodes_config = pipeline_graph.get('nodes', [])
            current_node_config = next((n for n in nodes_config if n.get('id') == node_run.node_id), {})
            node_data = current_node_config.get('data', {})
            
            ansible_task_id = node_data.get('ansible_task_id')
            if not ansible_task_id:
                raise ValueError("Ansible 节点未配置关联的任务 ID")
                
            from apps.task_management.models import AnsibleTask, AnsibleExecution, TaskLog
            from apps.task_management.tasks import run_ansible_task
            
            ansible_task = AnsibleTask.objects.get(id=ansible_task_id)
            node_run.logs = f"正在触发关联的 Ansible 任务: {ansible_task.name}\n"
            node_run.save()
            
            # 创建执行记录
            execution = AnsibleExecution.objects.create(
                task=ansible_task,
                status='pending',
                executor=node_run.run.trigger_user,
                from_pipeline=True  # 来自流水线，不发送单独通知
            )
            # 将执行实例 ID 存入节点输出，方便在中止流水线时反向查找并关停
            node_run.output_data = {'ansible_execution_id': execution.id}
            node_run.save(update_fields=['output_data'])

            # 同步执行底层任务逻辑（复用核心代码），传入构建好的产物上下文
            context_vars = {
                "pipeline_run_id": run_id,
                "pipeline_workspace": source_dir,
            }
            result = run_ansible_task(execution.id, extra_vars=context_vars)
            
            # 收集结果
            if isinstance(result, dict):
                node_run.logs += result.get('logs', '')
                if result.get('status') == 'success':
                    node_run.logs += "\nAnsible 执行成功！"
                    success = True
                else:
                    node_run.logs += f"\nAnsible 执行失败: {result.get('msg', '')}"
                    success = False
            else:
                # 降级方案 (防止莫名其妙的异常)
                execution.refresh_from_db()
                logs = TaskLog.objects.filter(execution=execution).order_by('create_time')
                node_run.logs += "\n".join([f"[{l.host}] {l.output}" for l in logs])
                success = (execution.status == 'success')
                
        elif node_type == 'k8s_deploy':
            pipeline_graph = node_run.run.pipeline.graph_data
            nodes_config = pipeline_graph.get('nodes', [])
            current_node_config = next((n for n in nodes_config if n.get('id') == node_run.node_id), {})
            node_data = current_node_config.get('data', {})

            cluster_id = node_data.get('k8s_cluster_id')
            release_name = node_data.get('k8s_release_name')
            namespace = node_data.get('k8s_namespace', 'default')
            chart_name = node_data.get('k8s_chart_name') # 新增 Chart 选择

            if not all([cluster_id, release_name]):
                raise ValueError("K8s 节点未配置完整的集群或 Release 名称")

            from apps.k8s_management.models import K8sCluster
            from apps.k8s_management.utils.helm_runner import run_helm_upgrade
            
            cluster = K8sCluster.objects.get(id=cluster_id)
            node_run.logs = f"集群: {cluster.name}, 正在执行 Helm Upgrade: {release_name} (Namespace: {namespace})...\n"
            node_run.save()
            
            # 尝试从上游节点获取动态生成的镜像信息
            upstream_nodes = PipelineNodeRun.objects.filter(run_id=run_id, status='success').exclude(output_data={})
            dynamic_repository = None
            dynamic_tag = None
            for un in upstream_nodes:
                if un.output_data and 'tag' in un.output_data:
                    dynamic_tag = un.output_data.get('tag')
                    dynamic_repository = un.output_data.get('repository')
                    node_run.logs += f"已扫描到上游镜像制品：{dynamic_repository}:{dynamic_tag}\n"
                    break
                    
            import time
            import yaml
            extra_values_dict = {
                "pipeline_redeploy_ts": int(time.time())
            }
            
            if dynamic_tag:
                extra_values_dict['image'] = {
                    'tag': dynamic_tag
                }
                if dynamic_repository:
                    extra_values_dict['image']['repository'] = dynamic_repository
            
            extra_values = yaml.dump(extra_values_dict)
            
            ok, output = run_helm_upgrade(
                cluster, 
                release_name, 
                namespace=namespace, 
                chart=chart_name, 
                values=extra_values,
                force=node_data.get('k8s_force', False)
            )
            node_run.logs += output
            success = ok
            
        elif node_type == 'http_webhook':
            pipeline_graph = node_run.run.pipeline.graph_data
            nodes_config = pipeline_graph.get('nodes', [])
            current_node_config = next((n for n in nodes_config if n.get('id') == node_run.node_id), {})
            node_data = current_node_config.get('data', {})
            
            url = node_data.get('webhook_url')
            method = node_data.get('webhook_method', 'POST')
            
            if not url:
               raise ValueError("Webhook 节点未配置 URL")
               
            node_run.logs = f"正在发起 Webhook 请求 ({method}): {url}...\n"
            node_run.save()
            
            try:
                # 传入一些 Pipeline 运行的相关元数据
                payload = { "run_id": node_run.run.id, "pipeline": node_run.run.pipeline.name, "node": node_run.node_label }
                if method == 'POST':
                    resp = requests.post(url, json=payload, timeout=10)
                else:
                    resp = requests.get(url, params=payload, timeout=10)
                    
                node_run.logs += f"HTTP {resp.status_code}\n"
                node_run.logs += resp.text[:1000] # avoid too much log
                success = (200 <= resp.status_code < 300)
            except Exception as e:
                node_run.logs += f"请求触发异常: {str(e)}"
                success = False
            
        else:
            node_run.logs = f"未知类型的节点: {node_type}，直接跳过或者当做正常处理"
            success = True

    except SoftTimeLimitExceeded:
        node_run.status = 'failed'
        node_run.logs = (node_run.logs or "") + "\n 节点执行超时 (Soft Time Limit Exceeded)。"
        node_run.end_time = timezone.now()
        node_run.save()
        success = False
    except Exception as e:
        node_run.logs = (node_run.logs or "") + f"\n执行过程中产生致命错误: {str(e)}"
        success = False

    # 善后处理
    if node_run.status == 'running':
        node_run.status = 'success' if success else 'failed'
        node_run.end_time = timezone.now()
        node_run.save()
        
    # --- 👑 核心：自动重试与容灾逻辑 ---
    if not success:
        # 从该节点的 graph_data 动态参数中读取配置（默认为 0 次重试）
        pipeline_graph = node_run.run.pipeline.graph_data
        nodes_config = pipeline_graph.get('nodes', [])
        current_node_config = next((n for n in nodes_config if n.get('id') == node_run.node_id), {})
        node_params = current_node_config.get('data', {})
        
        max_retries = int(node_params.get('max_retries', 0))
        retry_delay = int(node_params.get('retry_delay', 10)) # 默认失败后 10 秒重试
        
        if node_run.retry_count < max_retries:
            node_run.retry_count += 1
            node_run.status = 'running' # 回归运行中显示
            node_run.logs += f"\n\n🔥 [感知到故障] 准备进行第 {node_run.retry_count} 次自动重试 (计划上限: {max_retries} 次)...\n"
            node_run.save()
            
            # 手动推送中间态给前端
            push_pipeline_status_to_ws(node_run.run)
            
            # 延时重新下发该节点任务（直到真正宣告失败）
            from apps.pipeline_management.tasks import execute_pipeline_node
            execute_pipeline_node.apply_async(args=[node_run.id], countdown=retry_delay)
            return f"Retry Scheduled for Node {node_run_id}"

    # 实时推送：节点最终状态宣告
    push_pipeline_status_to_ws(node_run.run)

    # 只有当本节点真正宣告完成后（无论成败），通知继续扫描DAG
    advance_pipeline_engine.delay(run_id)

@shared_task(name='apps.pipeline_management.tasks.execute_pipeline_cron')
def execute_pipeline_cron(pipeline_id):
    """
    接收来自 Celery Beat (定期任务) 的触发。
    它负责替“系统”捏造一次触发并甩给 DAG 大脑。
    """
    try:
        pipeline = Pipeline.objects.get(id=pipeline_id)
        # 防止流水线被停用或者关闭了定时开关
        if not pipeline.is_active or not pipeline.is_cron_enabled:
            return "Skipped: Pipeline inactive or cron disabled"
            
        run = PipelineRun.objects.create(
            pipeline=pipeline,
            status='pending',
            trigger_user=None,  # 定时任务没有真实发起人，为空代表系统调度
            trigger_type='schedule'
        )
        # 将任务实体扔给主引擎继续调度
        advance_pipeline_engine.delay(run.id)
        return f"Cron Pipeline triggered: Run ID {run.id}"
    except Pipeline.DoesNotExist:
        return "Pipeline does not exist"


@shared_task(bind=True)
def advance_pipeline_engine(self, run_id):
    """
    流水线引擎(DAG Engine)- 大脑
    每次某个节点成功后被调用，或者初始化流水线时被调用。
    """
    run = PipelineRun.objects.get(id=run_id)
    
    # 记录大脑的任务 ID
    run.celery_task_id = self.request.id
    run.save()
    
    # 实时推送：大脑已接管（状态可能是 pending 或由于之前重入还是 running）
    push_pipeline_status_to_ws(run)
    
    # 如果处于 pending 状态，说明是首次进入引擎，发送启动通知（飞书/钉钉）
    if run.status == 'pending':
        from apps.system_management.notifiers import notify_pipeline_start
        try:
            notify_pipeline_start(run)
        except Exception:
            pass
    
    if run.status in ['success', 'failed', 'cancelled']:
        return # 已经终态的流水线直接返回
    
    pipeline = run.pipeline
    graph_data = pipeline.graph_data
    
    nodes_config = graph_data.get('nodes', [])
    edges_config = graph_data.get('edges', [])
    
    # 获取此 Run 已生成的所有节点状态字典
    node_runs = list(run.nodes.all())
    node_status_map = { nr.node_id: nr for nr in node_runs }
    
    # 第一步判断：如果初始化时没有任何 node_run 记录，先根据 graph_data 生成全部 pending 记录
    if not node_runs and nodes_config:
        new_records = []
        for nc in nodes_config:
            new_records.append(PipelineNodeRun(
                run=run,
                node_id=nc.get('id'),
                node_type=nc.get('type'),
                node_label=nc.get('data', {}).get('label', ''),
                status='pending'
            ))
        PipelineNodeRun.objects.bulk_create(new_records)
        # 刷新一遍 node_runs 和 node_status_map
        node_runs = list(run.nodes.all())
        node_status_map = { nr.node_id: nr for nr in node_runs }

    # 将 Run 状态置为 running（首次执行和重试执行都要设置）
    if run.status == 'pending':
        run.status = 'running'
        run.start_time = timezone.now()
        run.save()
        # 实时推送：流水线大脑初始化并启动
        push_pipeline_status_to_ws(run)

    # 重试时：从父运行复制工作区产物到新工作区（无论首次执行还是重试都要执行）
    if run.parent_run_id:
        parent_workspace = f"/tmp/ansflow_workspaces/run_{run.parent_run_id}"
        current_workspace = f"/tmp/ansflow_workspaces/run_{run_id}"
        if os.path.exists(parent_workspace) and not os.path.exists(current_workspace):
            import shutil
            # 复制父运行的工作区到新运行（保留 git clone 等产物）
            shutil.copytree(parent_workspace, current_workspace, dirs_exist_ok=True)

    # ======= 状态评估核心 =======
    
    # 检查是否有失败节点，有一个失败则整个 pipeline 失败 (默认开启 fail-fast)
    failed_nodes = [nr for nr in node_runs if nr.status == 'failed']
    if failed_nodes:
        run.status = 'failed'
        run.end_time = timezone.now()
        run.save()
        
        # 实时推送与通知
        push_pipeline_status_to_ws(run)
        
        from apps.system_management.notifiers import notify_pipeline_result
        try:
            notify_pipeline_result(run)
        except Exception:
            pass
        return

    # 寻找本轮所有准备就绪的节点
    # 规则：该节点自身处于 pending 状态，并且它"所有的"前置依赖节点都处于 success 状态
    
    ready_nodes = []
    has_running_or_pending = False

    for nr in node_runs:
        if nr.status in ['running']:
            has_running_or_pending = True
            
        if nr.status == 'pending':
            has_running_or_pending = True
            # 去连线里面找：哪些线的 target 是本节点？
            incoming_edges = [e for e in edges_config if e.get('target') == nr.node_id]
            
            # 如果没有前置依赖，代表这是首发节点
            if not incoming_edges:
                ready_nodes.append(nr)
                continue
            
            # 否则，检查所有上游节点的当前状态
            all_upstream_success = True
            for edge in incoming_edges:
                source_id = edge.get('source')
                source_run = node_status_map.get(source_id)
                if not source_run or source_run.status not in ('success', 'skipped'):
                    all_upstream_success = False
                    break
            
            if all_upstream_success:
                ready_nodes.append(nr)

    # 触发就绪的节点执行
    if ready_nodes:
        # 统一使用流水线的全局超时时间
        pipeline_timeout = run.pipeline.timeout or 3600
        
        # 为了防止并发条件下的重复派发，先将这些节点标记为 dispatched
        for nr in ready_nodes:
            nr.status = 'running' # 提前占坑，让后续大脑扫描不再视其为 pending
            nr.save(update_fields=['status'])
            # 使用 apply_async 下发任务并注入超时限制
            execute_pipeline_node.apply_async(args=[nr.id], soft_time_limit=pipeline_timeout)
        
        # 批量下发后实时推送一次总体进展
        push_pipeline_status_to_ws(run)
            
    else:
        # 如果没有就绪，判断是否是完全胜利结束了
        if not has_running_or_pending:
            # 说明所有的均不是 pending/running，只剩 success 了！
            run.status = 'success'
            run.end_time = timezone.now()
            run.save()
            
            # 实时推送：整条流水线胜利完成
            push_pipeline_status_to_ws(run)

            from apps.system_management.notifiers import notify_pipeline_result
            try:
                notify_pipeline_result(run)
            except Exception:
                pass
            
            # 流水线全局终态清理：清理挂载在宿主机的工作区
            import shutil
            workspace_dir = f"/tmp/ansflow_workspaces/run_{run_id}"
            shutil.rmtree(workspace_dir, ignore_errors=True)
