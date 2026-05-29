import logging
from celery import shared_task
from django.utils import timezone
from kubernetes import client
from .models import K8sCluster
from .utils.k8s_helper import get_k8s_client

logger = logging.getLogger(__name__)

@shared_task(name="sync_k8s_cluster_status")
def sync_k8s_cluster_status(cluster_id=None):
    """
    同步 K8s 集群状态：存活、版本、节点数、容量
    """
    if cluster_id:
        clusters = K8sCluster.objects.filter(id=cluster_id)
    else:
        clusters = K8sCluster.objects.all()

    for cluster in clusters:
        try:
            api_client = get_k8s_client(cluster)
            
            # 1. 获取版本信息
            version_api = client.VersionApi(api_client)
            v_info = version_api.get_code()
            cluster.version = f"v{v_info.major}.{v_info.minor}"

            # 2. 获取节点指标
            core_api = client.CoreV1Api(api_client)
            nodes = core_api.list_node()
            
            cluster.node_count = len(nodes.items)
            ready_nodes = 0
            total_cpu = 0
            total_mem_kb = 0

            for node in nodes.items:
                # 检查 Ready 状态
                is_ready = any(c.type == 'Ready' and c.status == 'True' for c in node.status.conditions)
                if is_ready:
                    ready_nodes += 1
                
                # 累加容量
                capacity = node.status.capacity
                # CPU 格式通常为 '8' 或 '8000m'
                cpu = capacity.get('cpu', '0')
                if 'm' in cpu:
                    total_cpu += int(cpu.replace('m', '')) / 1000
                else:
                    total_cpu += int(cpu)
                
                # 内存格式通常为 '16384Ki' 或 '16Gi'
                mem = capacity.get('memory', '0')
                if mem.endswith('Ki'):
                    total_mem_kb += int(mem.replace('Ki', ''))
                elif mem.endswith('Mi'):
                    total_mem_kb += int(mem.replace('Mi', '')) * 1024
                elif mem.endswith('Gi'):
                    total_mem_kb += int(mem.replace('Gi', '')) * 1024 * 1024

            cluster.ready_node_count = ready_nodes
            cluster.cpu_capacity = f"{round(total_cpu, 1)} Core"
            cluster.memory_capacity = f"{round(total_mem_kb / (1024 * 1024), 1)} GiB"
            
            cluster.status = 'running'
            cluster.error_message = ""
            cluster.last_seen = timezone.now()
            cluster.save()
            logger.info(f"K8s Cluster {cluster.name} status synced: {ready_nodes}/{len(nodes.items)} nodes ready.")

        except Exception as e:
            logger.error(f"Failed to sync K8s cluster {cluster.name}: {str(e)}")
            cluster.status = 'error'
            cluster.error_message = str(e)
            cluster.save()

    return f"Processed {clusters.count()} clusters."

@shared_task(name="sync_k8s_application")
def sync_k8s_application(app_id, force_sync=False):
    """
    GitOps 应用同步任务：
    1. 拉取 Git 仓库代码
    2. Helm Template 生成期望状态
    3. 获取 K8s API 实际状态并对比 (Drift Detection)
    4. 根据配置决定是否自动同步 (Self-healing) 或 手动强制同步 (force_sync)
    """
    from .models import K8sApplication
    import os
    import shutil
    import tempfile
    import subprocess
    import yaml
    from .utils.helm_runner import run_helm_template, run_helm_upgrade
    
    app = K8sApplication.objects.get(id=app_id)
    tmp_dir = tempfile.mkdtemp(prefix=f'gitops-{app.name}-')
    
    try:
        # 1. 克隆 Git 仓库 (优化：增加 --depth 1 减少流量，增加 SSH 忽略验证)
        env = os.environ.copy()
        env["GIT_SSH_COMMAND"] = "ssh -o UserKnownHostsFile=/dev/null -o StrictHostKeyChecking=no"
        
        cmd = ["git", "clone", "--depth", "1", "-b", app.git_branch, app.git_repo, tmp_dir]
        subprocess.run(cmd, check=True, capture_output=True, env=env)
        
        # 获取最新 Commit ID
        rev_cmd = ["git", "rev-parse", "HEAD"]
        rev_res = subprocess.run(rev_cmd, cwd=tmp_dir, capture_output=True, text=True)
        current_revision = rev_res.stdout.strip()
        
        chart_path = os.path.join(tmp_dir, app.path)
        
        # 2. 检查路径并提供回退机制 (Fallback to Built-in Chart)
        if not os.path.exists(chart_path):
            # 获取 AnsFlow 项目根目录下的内置 Chart 路径 (backend/deploy/helm/standard-app)
            builtin_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'deploy', 'helm', 'standard-app')
            if os.path.exists(builtin_path):
                logger.info(f"Chart path {chart_path} not found in repo. Falling back to built-in template: {builtin_path}")
                chart_path = builtin_path
            else:
                raise Exception(f"Chart path {app.path} not found in repo and built-in template also missing.")

        # 3. 解析 .ansflow-ci.yml 提取镜像更新 (image_updates)
        extra_values = {}
        ci_config_path = os.path.join(tmp_dir, '.ansflow-ci.yml')
        if os.path.exists(ci_config_path):
            try:
                with open(ci_config_path, 'r') as f:
                    ci_config = yaml.safe_load(f)
                    # 遍历 tasks 寻找 k8s-gitops 类型的任务
                    for task in ci_config.get('tasks', []):
                        if task.get('executor') == 'k8s-gitops' and 'image_updates' in task:
                            for update in task['image_updates']:
                                # 假设我们的 standard-app 模板结构是 image.tag
                                extra_values['image'] = {
                                    'tag': update['image'].split(':')[-1] if ':' in update['image'] else 'latest',
                                    'repository': ':'.join(update['image'].split(':')[:-1]) if ':' in update['image'] else update['image']
                                }
                                logger.info(f"Extracted image update from CI config: {update['image']}")
            except Exception as ye:
                logger.warning(f"Failed to parse .ansflow-ci.yml: {str(ye)}")

        # 4. 生成期望 Manifest (Expected State)
        ok, expected_manifest = run_helm_template(app.name, app.namespace, chart_path, values=extra_values)
        if not ok:
            raise Exception(f"Helm template failed: {expected_manifest}")
            
        # 5. 获取集群实际状态 (Live State) 并对比
        from .utils.helm_runner import get_temp_kubeconfig
        kubeconfig_path = get_temp_kubeconfig(app.cluster)
        
        live_manifest = ""
        get_cmd = ['helm', 'get', 'manifest', app.name, '-n', app.namespace, '--kubeconfig', kubeconfig_path]
        get_res = subprocess.run(get_cmd, capture_output=True, text=True)
        
        if os.path.exists(kubeconfig_path): os.remove(kubeconfig_path)

        def clean_manifest(m):
            """移除注释、空行和末尾空格以进行稳定对比"""
            if not m: return ""
            lines = [l.rstrip() for l in m.splitlines() if l.strip() and not l.strip().startswith('#')]
            return "\n".join(lines)
        
        if get_res.returncode == 0:
            live_manifest = get_res.stdout
            # 对比经过清理后的 Manifest
            cleaned_expected = clean_manifest(expected_manifest)
            cleaned_live = clean_manifest(live_manifest)
            
            if cleaned_expected == cleaned_live:
                app.sync_status = 'Synced'
                app.diff_details = {}
            else:
                app.sync_status = 'OutOfSync'
                # 记录详细的差异日志
                logger.info(f"Drift detected for {app.name}. Expected len: {len(cleaned_expected)}, Live len: {len(cleaned_live)}")
                import difflib
                diff = difflib.unified_diff(
                    cleaned_expected.splitlines(),
                    cleaned_live.splitlines(),
                    fromfile='Expected',
                    tofile='Live',
                    n=1
                )
                diff_text = "\n".join(list(diff)[:10])
                logger.warning(f"Diff snippets for {app.name}:\n{diff_text}")
                
                app.diff_details = {
                    "msg": "Manifest drift detected",
                    "diff": diff_text[:1000]
                }
                app.save(update_fields=['sync_status', 'diff_details'])
        else:
            app.sync_status = 'OutOfSync'
            app.diff_details = {"msg": "Application not found in cluster, pending initial sync"}
            app.save(update_fields=['sync_status', 'diff_details'])

        # 6. 执行同步 (Auto-sync 或 手动强制同步)
        if (app.auto_sync or force_sync) and app.sync_status == 'OutOfSync':
            logger.info(f"Syncing application {app.name} (Force: {force_sync})...")
            app.sync_status = 'Syncing'
            app.save(update_fields=['sync_status'])
            
            # 构造描述信息
            sync_desc = f"GitOps Auto-sync to {current_revision[:8]}"
            if extra_values.get('image', {}).get('tag'):
                sync_desc += f" (Image: {extra_values['image']['tag']})"

            ok, output = run_helm_upgrade(app.cluster, app.name, app.namespace, chart_path, 
                                          values=yaml.dump(extra_values), description=sync_desc)
            if ok:
                app.sync_status = 'Synced'
                app.last_sync_time = timezone.now()
                app.last_sync_revision = current_revision
            else:
                app.sync_status = 'Error'
                app.error_message = f"Auto-sync failed: {output}"
            app.save()
        
        app.last_sync_time = timezone.now()
        app.error_message = ""
        app.save()
        
    except Exception as e:
        logger.error(f"GitOps Sync Error for {app.name}: {str(e)}")
        app.sync_status = 'Error'
        app.error_message = str(e)
        app.save()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return f"App {app.name} sync processed. Status: {app.sync_status}"
