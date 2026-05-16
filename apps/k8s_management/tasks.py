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
