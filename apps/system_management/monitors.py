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
        from django.core.cache import cache
        
        # 优先从后台定时任务生成的缓存中读取
        cache_key = "ansflow:system:celery_stats"
        cached_data = cache.get(cache_key)
        
        if cached_data:
            workers = cached_data.get('workers', [])
            active_workers = len([w for w in workers if w['status'] == 'online'])
            queue_length = 0
            if cached_data.get('queues'):
                queue_length = sum(q.get('length', 0) for q in cached_data['queues'])
        else:
            # 如果缓存不存在，则进行一次轻量级检查（不推荐在高频访问下使用）
            from config.celery import app
            from django_redis import get_redis_connection
            try:
                i = app.control.inspect(timeout=1.0) # 缩短超时
                pings = i.ping()
                active_workers = len(pings) if pings else 0
                conn = get_redis_connection("default")
                queue_length = conn.llen('celery')
            except:
                active_workers = 0
                queue_length = 0

        status = "healthy"
        if active_workers == 0:
            status = "unhealthy"
        elif queue_length > 100:
            status = "warning"

        return {
            "active_workers": active_workers,
            "queue_length": queue_length,
            "status": status,
            "info": f"在线 Worker: {active_workers}, 队列积压: {queue_length}"
        }

class CeleryBeatMonitor(BaseMonitor):
    name = "celery_beat"
    label = "定时调度引擎"
    icon = "ClockCircleOutlined"

    def perform_check(self):
        from django_celery_beat.models import PeriodicTask
        
        # 启发式检查：检查是否有定期任务在过去 5 分钟内被调度
        recent_tasks = PeriodicTask.objects.filter(enabled=True, last_run_at__isnull=False)
        beat_active = False
        last_run_time = None
        
        if recent_tasks.exists():
            last_run_task = recent_tasks.order_by('-last_run_at').first()
            last_run_time = last_run_task.last_run_at
            if last_run_time and (timezone.now() - last_run_time).total_seconds() < 300:
                beat_active = True
        
        return {
            "status": "healthy" if beat_active else "warning",
            "last_run": last_run_time.isoformat() if last_run_time else None,
            "info": f"调度状态: {'正常' if beat_active else '疑似离线 (5分钟内无调度)'}"
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
        CeleryBeatMonitor(),
        KubernetesMonitor()
    ]
    
    @classmethod
    def get_all_health(cls):
        results = []
        for monitor in cls._monitors:
            results.append(monitor.check())
        return results
