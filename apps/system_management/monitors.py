import time
import datetime
import logging
from django.db import connection
from django_redis import get_redis_connection
from django.conf import settings
from kubernetes import client as k8s_client
from ..k8s_management.models import K8sCluster
from ..k8s_management.utils.k8s_helper import get_k8s_client

logger = logging.getLogger(__name__)

class BaseMonitor:
    """健康检查基类"""
    name = "unknown"
    label = "未知组件"
    icon = "PlugOutlined"
    
    def check(self):
        start_time = time.time()
        try:
            result = self.perform_check()
            latency = f"{int((time.time() - start_time) * 1000)}ms"
            return {
                "name": self.name,
                "label": self.label,
                "icon": self.icon,
                "status": "healthy",
                "latency": latency,
                **result
            }
        except Exception as e:
            return {
                "name": self.name,
                "label": self.label,
                "icon": self.icon,
                "status": "unhealthy",
                "message": str(e)
            }

    def perform_check(self) -> dict:
        raise NotImplementedError

class DatabaseMonitor(BaseMonitor):
    name = "database"
    label = "数据库"
    icon = "DatabaseOutlined"

    def perform_check(self):
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        return {
            "type": connection.vendor,
            "info": f"引擎: {connection.settings_dict.get('ENGINE', '').split('.')[-1]}"
        }

class RedisMonitor(BaseMonitor):
    name = "redis"
    label = "Redis / MQ"
    icon = "ThunderboltOutlined"

    def perform_check(self):
        # 兼容性检测：优先检测 Celery Broker
        broker_url = getattr(settings, 'CELERY_BROKER_URL', '')
        conn = get_redis_connection("default")
        info = conn.info()
        return {
            "version": info.get('redis_version'),
            "used_memory": info.get('used_memory_human'),
            "clients": info.get('connected_clients'),
            "broker": "Redis" if "redis://" in broker_url else "Other"
        }

class CeleryMonitor(BaseMonitor):
    name = "celery"
    label = "任务执行引擎"
    icon = "RobotOutlined"

    def perform_check(self):
        from config.celery import app
        # 耗时操作，增加超时控制
        i = app.control.inspect(timeout=1.0)
        pings = i.ping()
        active_workers = len(pings) if pings else 0
        
        # 统计在线 Worker
        return {
            "active_workers": active_workers,
            "status": "healthy" if active_workers > 0 else "warning",
            "info": f"在线节点: {active_workers}"
        }

class KubernetesMonitor(BaseMonitor):
    name = "k8s"
    label = "容器化基础设施"
    icon = "ClusterOutlined"

    def perform_check(self):
        # 检测默认集群或是第一个集群的连通性
        cluster = K8sCluster.objects.first()
        if not cluster:
            return {"status": "warning", "info": "未配置集群"}
        
        k8s_api = get_k8s_client(cluster)
        api_instance = k8s_client.VersionApi(k8s_api)
        v = api_instance.get_code()
        return {
            "cluster_name": cluster.name,
            "version": v.git_version,
            "platform": v.platform
        }

class SystemHealthManager:
    """
    系统健康管理器：动态调度所有子 Monitor
    """
    
    _monitors = [
        DatabaseMonitor(),
        RedisMonitor(),
        CeleryMonitor(),
        KubernetesMonitor()
    ]
    
    @classmethod
    def get_all_health(cls):
        results = []
        for monitor in cls._monitors:
            results.append(monitor.check())
        return results
