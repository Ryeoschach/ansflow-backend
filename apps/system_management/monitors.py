import time
import datetime
import logging
import subprocess
from django.utils import timezone
from django.db import connection
from django_redis import get_redis_connection
from django.conf import settings
from ..k8s_management.models import K8sCluster

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
        from django_celery_beat.models import PeriodicTask
        from django_redis import get_redis_connection
        
        # 1. 检查 Worker 连通性
        i = app.control.inspect(timeout=3.0)
        pings = i.ping()
        active_workers = len(pings) if pings else 0
        
        # 2. 检查 Beat 存活状态 (通过检测是否有定期任务在过去 5 分钟内被调度)
        # 这是一个启发式检查，如果没有任何任务在运行，可能无法准确判断，但通常系统会有内置的心跳任务
        recent_tasks = PeriodicTask.objects.filter(enabled=True, last_run_at__isnull=False)
        beat_active = False
        if recent_tasks.exists():
            last_run = recent_tasks.order_by('-last_run_at').first().last_run_at
            if last_run and (timezone.now() - last_run).total_seconds() < 300:
                beat_active = True
        
        # 3. 检查队列积压 (从 Redis 读取)
        queue_length = 0
        try:
            conn = get_redis_connection("default")
            # 默认队列名称通常为 'celery'
            queue_length = conn.llen('celery')
        except Exception as e:
            logger.warning(f"Failed to get queue length: {e}")

        status = "healthy"
        if active_workers == 0:
            status = "unhealthy"
        elif not beat_active or queue_length > 100:
            status = "warning"

        return {
            "active_workers": active_workers,
            "beat_status": "online" if beat_active else "offline",
            "queue_length": queue_length,
            "status": status,
            "info": f"Worker: {active_workers}, Beat: {'在线' if beat_active else '离线'}, 积压: {queue_length}"
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

        try:
            # 优先用 ~/.kube/config 中的当前上下文 server 地址
            server = cluster.api_server
            if not server:
                return {"status": "warning", "info": "未配置 API Server"}

            result = subprocess.run(
                ["curl", "-sk", "--max-time", "5", "-o", "/dev/null", "-w", "%{http_code}|%{time_total}",
                 f"{server}/version/"],
                capture_output=True, text=True, timeout=8
            )
            output = result.stdout.strip()
            if "|" in output:
                http_code, time_total = output.split("|")
                if http_code in ("200", "401", "403"):
                    return {
                        "status": "healthy",
                        "info": f"连接正常 ({server})"
                    }
                return {"status": "warning", "info": f"K8s 返回: HTTP {http_code}"}
            return {"status": "warning", "info": f"K8s 连接超时"}
        except subprocess.TimeoutExpired:
            return {"status": "warning", "info": f"K8s 连接超时: {cluster.name}"}
        except Exception as e:
            return {"status": "unhealthy", "info": f"K8s 检查失败: {str(e)[:50]}"}

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
