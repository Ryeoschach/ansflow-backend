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
def sync_k8s_application(app_id):
    """
    GitOps 应用同步任务：
    1. 拉取 Git 仓库代码
    2. Helm Template 生成期望状态
    3. 获取 K8s API 实际状态并对比 (Drift Detection)
    4. 根据配置决定是否自动同步 (Self-healing)
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
        # 1. 克隆 Git 仓库
        cmd = ["git", "clone", "-b", app.git_branch, app.git_repo, tmp_dir]
        subprocess.run(cmd, check=True, capture_output=True)
        
        # 获取最新 Commit ID
        rev_cmd = ["git", "rev-parse", "HEAD"]
        rev_res = subprocess.run(rev_cmd, cwd=tmp_dir, capture_output=True, text=True)
        current_revision = rev_res.stdout.strip()
        
        chart_path = os.path.join(tmp_dir, app.path)
        
        # 2. 生成期望 Manifest (Expected State)
        ok, expected_manifest = run_helm_template(app.name, app.namespace, chart_path)
        if not ok:
            raise Exception(f"Helm template failed: {expected_manifest}")
            
        # 3. 获取集群实际状态 (Live State) 并对比
        # 这里简化处理：我们使用 helm get manifest 来获取该 Release 的实际清单
        # 在 ArgoCD 中是对比具体资源对象，我们这里采用 Helm 层面的一致性校验
        from .utils.k8s_helper import get_k8s_client
        api_client = get_k8s_client(app.cluster)
        
        # 为了实现真正的 Drift Detection，我们需要调用 helm get manifest
        # 注意：这需要集群中已经安装了该应用
        from .utils.helm_runner import get_temp_kubeconfig
        kubeconfig_path = get_temp_kubeconfig(app.cluster)
        
        live_manifest = ""
        get_cmd = ['helm', 'get', 'manifest', app.name, '-n', app.namespace, '--kubeconfig', kubeconfig_path]
        get_res = subprocess.run(get_cmd, capture_output=True, text=True)
        
        if os.path.exists(kubeconfig_path): os.remove(kubeconfig_path)
        
        if get_res.returncode == 0:
            live_manifest = get_res.stdout
            # 对比 Manifest (简单的字符串或结构化对比)
            if expected_manifest.strip() == live_manifest.strip():
                app.sync_status = 'Synced'
                app.diff_details = {}
            else:
                app.sync_status = 'OutOfSync'
                # 记录简要 Diff (可以通过第三方库如 dictdiff 增强)
                app.diff_details = {"msg": "Manifest drift detected between Git and Cluster"}
        else:
            # 应用可能还未安装
            app.sync_status = 'OutOfSync'
            app.diff_details = {"msg": "Application not found in cluster, pending initial sync"}

        # 4. 自动同步 (Self-healing)
        if app.auto_sync and app.sync_status == 'OutOfSync':
            logger.info(f"Auto-syncing application {app.name}...")
            # 直接调用现有的 Helm Upgrade 逻辑
            # 注意：此处暂不处理复杂的 values.yaml，仅使用 Git 仓库中的默认值
            ok, output = run_helm_upgrade(app.cluster, app.name, app.namespace, chart_path)
            if ok:
                app.sync_status = 'Synced'
                app.last_sync_time = timezone.now()
                app.last_sync_revision = current_revision
            else:
                app.sync_status = 'Error'
                app.error_message = f"Auto-sync failed: {output}"

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
