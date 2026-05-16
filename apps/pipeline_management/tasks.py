import time
import os
import logging
import requests
import subprocess
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

def push_node_log_to_ws(run_id, node_id, log_content):
    """
    增量推送节点日志给前端
    """
    if not log_content: return
    channel_layer = get_channel_layer()
    async_to_sync(channel_layer.group_send)(
        f"pipeline_run_{run_id}",
        {
            "type": "pipeline_node_log_append",
            "data": {
                "node_id": node_id,
                "content": log_content
            }
        }
    )

def run_command_with_streaming_logs(cmd, node_run, cwd=None):
    """
    执行命令并实时流式推送日志
    """
    # 打印执行命令本身
    start_msg = f"[$] {' '.join(cmd) if isinstance(cmd, list) else cmd}\n"
    node_run.logs = (node_run.logs or "") + start_msg
    node_run.save(update_fields=['logs'])
    push_node_log_to_ws(node_run.run_id, node_run.node_id, start_msg)

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        universal_newlines=True,
        cwd=cwd
    )

    # 累计本次执行的所有新日志
    current_exec_logs = ""
    # 实时读取
    for line in process.stdout:
        if line:
            current_exec_logs += line
            # 推送到 WebSocket
            push_node_log_to_ws(node_run.run_id, node_run.node_id, line)
            
    process.wait()
    
    # 最后将本次执行的日志追加回 node_run.logs 并持久化
    node_run.logs = (node_run.logs or "") + current_exec_logs
    node_run.save(update_fields=['logs'])
    
    return process.returncode == 0

@shared_task(bind=True)
def execute_pipeline_node(self, node_run_id):
    """
    具体的单个节点执行器，执行完后不管成败，均回调引擎继续决策调度
    """
    logger.info(f"🚀 开始执行节点任务: node_run_id={node_run_id}")
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
    # 并行隔离：为每个节点分配独立的子目录
    parent_run_id = node_run.run.parent_run_id
    base_workspace = f"/tmp/ansflow_workspaces/run_{parent_run_id or run_id}"
    node_workspace = os.path.join(base_workspace, f"node_{node_run.node_id}")
    os.makedirs(node_workspace, exist_ok=True)
    
    # 源代码目录（共享或按需克隆）
    source_dir = os.path.join(base_workspace, 'source')
    
    # 准备节点配置数据
    pipeline_graph = node_run.run.pipeline.graph_data
    if isinstance(pipeline_graph, str):
        import json
        pipeline_graph = json.loads(pipeline_graph)
    nodes_config = pipeline_graph.get('nodes', [])
    current_node_config = next((n for n in nodes_config if n.get('id') == node_run.node_id), {})
    
    # --- 核心：变量解析与注入 ---
    from .utils import resolve_pipeline_vars
    raw_node_data = current_node_config.get('data', {})
    node_data = resolve_pipeline_vars(raw_node_data, run_id)
    
    try:
        # ---- 根据 node_type 进行不同业务分流 ----
        node_type = node_run.node_type
        
        if node_type == 'input':
            node_run.logs = "起点触发完成。"
            success = True
            
        elif node_type == 'git_clone':
            repo_url = node_data.get('git_repo')
            branch = node_data.get('git_branch', 'main')
            
            if not repo_url:
                raise ValueError("Git 节点未配置仓库地址(URL)")
                
            init_msg = f"准备克隆拉取代码: {repo_url} (分支: {branch})...\n工作区挂载: {source_dir}\n"
            node_run.logs = (node_run.logs or "") + init_msg
            node_run.save(update_fields=['logs'])
            push_node_log_to_ws(node_run.run_id, node_run.node_id, init_msg)
            
            # 保证拉取前目录干净
            if os.path.exists(source_dir):
                shutil.rmtree(source_dir, ignore_errors=True)
                
            cmd = ["git", "clone", "-b", branch, repo_url, source_dir]
            success = run_command_with_streaming_logs(cmd, node_run)
            if success:
                finish_msg = "\n✨ 代码拉取成功！已放入统一工作区。"
                node_run.logs += finish_msg
                node_run.save(update_fields=['logs'])
                push_node_log_to_ws(node_run.run_id, node_run.node_id, finish_msg)
                
        elif node_type == 'docker_build':
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
            
            init_msg = f"正在启动 Docker 容器沙箱编译...\n> 工作区映射: {source_dir} -> /workspace\n> 拉起底层镜像: {image_name}\n> 注入的构建指令:\n{build_script}\n"
            node_run.logs = (node_run.logs or "") + init_msg
            node_run.save(update_fields=['logs'])
            push_node_log_to_ws(node_run.run_id, node_run.node_id, init_msg)
            
            if not os.path.exists(source_dir):
                raise ValueError("代码工作区为空，请检查本节点上方是否正确连接了 Git 拉取节点！")
                
            # --rm: 用完即毁, -v: 挂载代码, -w: 切换工作目录
            cmd = [
                "docker", "run", "--rm",
                "-v", f"{source_dir}:/workspace",
                "-w", "/workspace",
                image_name,
                "/bin/sh", "-c", build_script
            ]
            
            success = run_command_with_streaming_logs(cmd, node_run)
            if success:
                finish_msg = "\n✨ 隔离沙箱编译执行成功！所有编译产物均已落回宿主机的工作区中。"
                node_run.logs += finish_msg
                node_run.save(update_fields=['logs'])
                push_node_log_to_ws(node_run.run_id, node_run.node_id, finish_msg)
            else:
                fail_msg = "\n❌ 沙箱编译失败。"
                node_run.logs += fail_msg
                node_run.save(update_fields=['logs'])
                push_node_log_to_ws(node_run.run_id, node_run.node_id, fail_msg)

        elif node_type == 'kaniko_build':
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

            # 格式化镜像名称与 Tag
            if ":" in image_name:
                parts = image_name.split(":", 1)
                real_name = parts[0]
                if image_tag and image_tag != f"v{run_id}":
                    image_name = real_name
                else:
                    image_name = real_name
                    image_tag = parts[1]

            image_name = image_name.strip(":")
            image_tag = image_tag.strip(":")

            if registry.namespace:
                full_image = f"{push_host}/{registry.namespace}/{image_name}:{image_tag}"
            else:
                full_image = f"{push_host}/{image_name}:{image_tag}"
            
            init_msg = f"🚀 正在启动 Kaniko 容器进行镜像构建并推送...\n> 目标镜像: {full_image}\n> Dockerfile: {dockerfile_path}\n"
            node_run.logs = (node_run.logs or "") + init_msg
            node_run.save(update_fields=['logs'])
            push_node_log_to_ws(node_run.run_id, node_run.node_id, init_msg)
            
            auth_string = f"{registry.username}:{registry.password}"
            auth_b64 = base64.b64encode(auth_string.encode('utf-8')).decode('utf-8')
            kaniko_dir = os.path.join(node_workspace, '.kaniko') # 使用隔离目录
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
                
            cmd = [
                "docker", "run", "--rm",
                "-v", f"{source_dir}:/workspace",
                "-v", f"{config_json_path}:/kaniko/.docker/config.json",
                "gcr.io/kaniko-project/executor:debug",
                "--context", f"dir:///workspace/{context_path}",
                "--dockerfile", f"/workspace/{dockerfile_path}",
                "--destination", full_image
            ]

            success = run_command_with_streaming_logs(cmd, node_run)
            
            if success:
                finish_msg = f"\n✨ Kaniko 镜像构建成功！并已推送到: {full_image}"
                node_run.logs += finish_msg
                node_run.save(update_fields=['logs'])
                push_node_log_to_ws(node_run.run_id, node_run.node_id, finish_msg)
                
                node_run.output_data = {
                    "repository": full_image.split(':')[0],
                    "tag": image_tag,
                    "full_image": full_image
                }
                node_run.save(update_fields=['output_data'])

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
                    node_run.save(update_fields=['logs'])
                except Exception as art_err:
                    node_run.logs += f"\n[产物记录] 记录失败: {str(art_err)}"
                    node_run.save(update_fields=['logs'])
            else:
                node_run.logs += "\n❌ Kaniko 编译失败。"
                node_run.save(update_fields=['logs'])
                success = False
            
        elif node_type == 'ansible':
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
            cluster_id = node_data.get('k8s_cluster_id')
            release_name = node_data.get('k8s_release_name')
            namespace = node_data.get('k8s_namespace', 'default')
            chart_name = node_data.get('k8s_chart_name')
            repo_id = node_data.get('k8s_repo_id') # 新增仓库 ID

            if not all([cluster_id, release_name]):
                raise ValueError("K8s 节点未配置完整的集群或 Release 名称")

            from apps.k8s_management.models import K8sCluster, HelmRepository
            from apps.k8s_management.utils.helm_runner import run_helm_upgrade

            cluster = K8sCluster.objects.get(id=cluster_id)
            node_run.logs = f"集群: {cluster.name}, 正在执行 Helm Upgrade: {release_name} (Namespace: {namespace})...\n"

            # 处理远程仓库信息
            repo_url = None
            repo_auth = None
            if repo_id:
                try:
                    repo_obj = HelmRepository.objects.get(id=repo_id)
                    repo_url = repo_obj.url
                    if repo_obj.username:
                        repo_auth = {'username': repo_obj.username, 'password': repo_obj.password}
                    node_run.logs += f"使用远程仓库: {repo_obj.name} ({repo_url})\n"
                except HelmRepository.DoesNotExist:
                    node_run.logs += f"警告: 指定的 Helm 仓库 (ID:{repo_id}) 不存在，尝试回退到本地/自动探测模式。\n"

            node_run.save()

            # 变量注入与智能扫描 (保持原有逻辑)
            dynamic_tag = node_data.get('image_tag')
            dynamic_repository = node_data.get('image_repository')

            if not dynamic_tag:
                upstream_nodes = PipelineNodeRun.objects.filter(run_id=run_id, status='success').exclude(output_data={})
                for un in upstream_nodes:
                    if un.output_data and 'tag' in un.output_data:
                        dynamic_tag = un.output_data.get('tag')
                        dynamic_repository = un.output_data.get('repository')
                        node_run.logs += f"已通过智能扫描获取到上游镜像：{dynamic_repository}:{dynamic_tag}\n"
                        break

            import time
            import yaml
            extra_values_dict = { "pipeline_redeploy_ts": int(time.time()) }
            if dynamic_tag:
                extra_values_dict['image'] = { 'tag': dynamic_tag }
                if dynamic_repository: extra_values_dict['image']['repository'] = dynamic_repository

            # 合并用户自定义 values
            custom_values = node_data.get('k8s_values', '')
            if custom_values:
                node_run.logs += f"\n[审计] 注入自定义 Values:\n{custom_values}\n"

            full_values = yaml.dump(extra_values_dict) + "\n" + custom_values

            ok, output = run_helm_upgrade(
                cluster, 
                release_name, 
                namespace=namespace, 
                chart=chart_name, 
                values=full_values,
                force=node_data.get('k8s_force', False),
                repo_url=repo_url,
                repo_auth=repo_auth
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
    logger.info(f"🧠 流水线引擎已唤醒: run_id={run_id}")
    try:
        run = PipelineRun.objects.get(id=run_id)
        
        # 记录大脑的任务 ID
        run.celery_task_id = self.request.id
        run.save(update_fields=['celery_task_id'])
        
        if run.status in ['success', 'failed', 'cancelled']:
            return 
        
        pipeline = run.pipeline
        graph_data = pipeline.graph_data or {}
        if not isinstance(graph_data, dict):
            import json
            if isinstance(graph_data, str):
                try:
                    graph_data = json.loads(graph_data)
                except: graph_data = {}
            else: graph_data = {}

        nodes_config = graph_data.get('nodes', [])
        edges_config = graph_data.get('edges', [])
        
        if not nodes_config:
            run.status = 'failed'
            run.save(update_fields=['status'])
            push_pipeline_status_to_ws(run)
            logger.error(f"流水线 #{run_id} 没有任何节点配置，无法执行。")
            return

        # 获取此 Run 已生成的所有节点 ID 集合
        existing_node_ids = set(run.nodes.values_list('node_id', flat=True))
        
        # 初始化缺失的节点记录
        new_records = []
        for nc in nodes_config:
            if nc.get('id') not in existing_node_ids:
                new_records.append(PipelineNodeRun(
                    run=run,
                    node_id=nc.get('id'),
                    node_type=nc.get('type'),
                    node_label=nc.get('data', {}).get('label', ''),
                    status='pending'
                ))
        
        if new_records:
            try:
                # ignore_conflicts=True 即使并发写入也能保证不崩溃
                PipelineNodeRun.objects.bulk_create(new_records, ignore_conflicts=True)
            except Exception as be:
                logger.warning(f"批量创建节点时发生冲突（可能由于并发）：{str(be)}")

        # 重新获取完整的节点运行列表（确保包含刚创建的和已有的）
        node_runs = list(run.nodes.all())
        node_status_map = { nr.node_id: nr for nr in node_runs }

        # --- 👑 核心：状态流转优先 ---
        is_initial_start = (run.status == 'pending')
        if is_initial_start:
            run.status = 'running'
            run.start_time = timezone.now()
            run.save(update_fields=['status', 'start_time'])
            push_pipeline_status_to_ws(run)
            
            try:
                from apps.system_management.notifiers import notify_pipeline_start
                notify_pipeline_start(run)
            except Exception as e:
                logger.error(f"[Notify Error] 启动通知失败: {str(e)}")

        # 重试时：从父运行复制工作区产物
        if run.parent_run_id:
            parent_workspace = f"/tmp/ansflow_workspaces/run_{run.parent_run_id}"
            current_workspace = f"/tmp/ansflow_workspaces/run_{run_id}"
            if os.path.exists(parent_workspace) and not os.path.exists(current_workspace):
                import shutil
                shutil.copytree(parent_workspace, current_workspace, dirs_exist_ok=True)

        # ======= 状态评估核心 =======
        # ... (后续逻辑不变，但放入 try 中)
        # 寻找就绪节点
        ready_nodes = []
        has_running_or_pending = False

        for nr in node_runs:
            if nr.status == 'running':
                has_running_or_pending = True
            elif nr.status == 'pending':
                has_running_or_pending = True
                incoming_edges = [e for e in edges_config if e.get('target') == nr.node_id]
                
                if not incoming_edges:
                    ready_nodes.append(nr)
                else:
                    all_upstream_success = True
                    for edge in incoming_edges:
                        source_id = edge.get('source')
                        source_run = node_status_map.get(source_id)
                        if not source_run or source_run.status not in ('success', 'skipped'):
                            all_upstream_success = False
                            break
                    if all_upstream_success:
                        ready_nodes.append(nr)

        # 触发就绪节点
        if ready_nodes:
            pipeline_timeout = run.pipeline.timeout or 3600
            for nr in ready_nodes:
                if nr.node_type == 'approval':
                    # 审批节点：置为等待状态，暂停执行，等待人工通过 API 恢复
                    nr.status = 'waiting'
                    nr.save(update_fields=['status'])
                    logger.info(f"⏳ 节点 {nr.node_id} (Approval) 进入等待审批状态")
                else:
                    nr.status = 'running'
                    nr.save(update_fields=['status'])
                    execute_pipeline_node.apply_async(args=[nr.id], soft_time_limit=pipeline_timeout)
            push_pipeline_status_to_ws(run)
                
        elif not has_running_or_pending:
            # 检查是否有节点失败
            has_failed = any(nr.status == 'failed' for nr in node_runs)
            if has_failed:
                run.status = 'failed'
            else:
                run.status = 'success'
                # --- AI 知识闭环：自动摘要 ---
                if run.pipeline.auto_kb_summary:
                    try:
                        from apps.ai_engine.tasks import auto_summarize_run_task
                        auto_summarize_run_task.delay(run.id)
                        logger.info(f"✨ 已触发流水线 #{run_id} 自动知识总结")
                    except Exception as ai_err:
                        logger.error(f"无法触发 AI 总结: {str(ai_err)}")
                # --- End AI ---

            run.end_time = timezone.now()
            run.save(update_fields=['status', 'end_time'])
            push_pipeline_status_to_ws(run)

            try:
                from apps.system_management.notifiers import notify_pipeline_result
                notify_pipeline_result(run)
            except Exception: pass
            
            import shutil
            workspace_dir = f"/tmp/ansflow_workspaces/run_{run_id}"
            shutil.rmtree(workspace_dir, ignore_errors=True)

    except Exception as e:
        import traceback
        logger.error(f"❌ 流水线引擎崩溃 [Run #{run_id}]: {str(e)}\n{traceback.format_exc()}")
        try:
            run = PipelineRun.objects.get(id=run_id)
            run.status = 'failed'
            run.save(update_fields=['status'])
            push_pipeline_status_to_ws(run)
        except: pass


@shared_task(name='apps.pipeline_management.tasks.cleanup_old_workspaces')
def cleanup_old_workspaces(days=1):
    """
    定期清理过期的工作空间目录。
    默认清理 1 天前创建的目录。
    """
    import shutil
    import time
    
    base_dir = "/tmp/ansflow_workspaces"
    if not os.path.exists(base_dir):
        return "Workspace directory does not exist."
        
    now = time.time()
    seconds = days * 86400
    cleaned_count = 0
    
    for dirname in os.listdir(base_dir):
        # 仅处理形如 run_{id} 的目录
        if not dirname.startswith("run_"):
            continue
            
        dir_path = os.path.join(base_dir, dirname)
        if not os.path.isdir(dir_path):
            continue
            
        # 检查目录的修改时间
        if (now - os.path.getmtime(dir_path)) > seconds:
            try:
                # 尝试解析 ID 以检查流水线是否真的结束了 (可选，这里采用更通用的时间判断)
                run_id = dirname.replace("run_", "")
                if run_id.isdigit():
                    run = PipelineRun.objects.filter(id=int(run_id)).first()
                    # 如果流水线还在运行中，暂时跳过清理，除非目录实在太老了（如超过 3 天）
                    if run and run.status == 'running' and (now - os.path.getmtime(dir_path)) < (seconds * 3):
                        continue
                
                shutil.rmtree(dir_path)
                cleaned_count += 1
                logger.info(f"[Cleanup] Deleted expired workspace: {dir_path}")
            except Exception as e:
                logger.error(f"[Cleanup] Failed to delete {dir_path}: {str(e)}")
                
    return f"Cleaned up {cleaned_count} workspaces."
