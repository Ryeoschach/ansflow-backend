import logging
from celery import shared_task
from django.utils import timezone
from django.core.cache import cache
from config.celery import app as celery_app
from django_redis import get_redis_connection

logger = logging.getLogger(__name__)

@shared_task(name="apps.system_management.tasks.collect_celery_stats")
def collect_celery_stats():
    """
    后台异步采集 Celery 统计信息并更新缓存。
    解耦 API 响应，解决同步广播导致的延迟。
    """
    cache_key = "ansflow:system:celery_stats"
    
    try:
        # 允许稍长的超时，因为是后台任务
        inspector = celery_app.control.inspect(timeout=3.0)

        # 1. 基础任务统计 (批量发起)
        active = inspector.active() or {}
        scheduled = inspector.scheduled() or {}
        reserved = inspector.reserved() or {}
        stats = inspector.stats() or {}
        
        # 2. 汇总 Worker 信息
        worker_details = []
        all_workers = set(list(active.keys()) + list(scheduled.keys()) + list(reserved.keys()) + list(stats.keys()))
        
        for worker in all_workers:
            w_stats = stats.get(worker, {})
            worker_details.append({
                "worker": worker,
                "status": "online" if worker in active or worker in stats else "offline",
                "active_count": len(active.get(worker, [])),
                "scheduled_count": len(scheduled.get(worker, [])),
                "reserved_count": len(reserved.get(worker, [])),
                "concurrency": w_stats.get('pool', {}).get('max-concurrency'),
                "broker_transport": w_stats.get('broker', {}).get('transport'),
                "rusage": w_stats.get('rusage', {})
            })
        
        # 3. 队列积压情况 (从 Redis 获取)
        queue_stats = []
        try:
            conn = get_redis_connection("default")
            for q_name in ['celery']:
                queue_stats.append({
                    "name": q_name,
                    "length": conn.llen(q_name)
                })
        except Exception: pass
        
        # 4. Beat 状态
        from django_celery_beat.models import PeriodicTask
        recent_task = PeriodicTask.objects.filter(enabled=True, last_run_at__isnull=False).order_by('-last_run_at').first()
        beat_info = {
            "status": "offline",
            "last_run": None
        }
        if recent_task and recent_task.last_run_at:
            if (timezone.now() - recent_task.last_run_at).total_seconds() < 300:
                beat_info["status"] = "online"
            beat_info["last_run"] = recent_task.last_run_at.isoformat()
            
        result = {
            "workers": worker_details,
            "queues": queue_stats,
            "beat": beat_info,
            "timestamp": timezone.now().isoformat()
        }
        
        # 写入长效缓存 (建议 2 分钟过期，防止任务挂掉后数据太陈旧)
        cache.set(cache_key, result, 120)
        logger.info("[Monitor] Celery stats collected and cached successfully.")
        return "Success"

    except Exception as e:
        logger.error(f"[Monitor] Failed to collect Celery stats: {str(e)}")
        return f"Error: {str(e)}"
