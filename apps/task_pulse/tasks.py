import logging
from datetime import timedelta
from django.utils import timezone
from celery import shared_task
from .models import WorkerNode, TaskPulse

logger = logging.getLogger(__name__)

@shared_task(name='task_pulse.maintenance')
def pulse_maintenance_task():
    """
    TaskPulse 核心维护任务：
    1. 清理过期的数据 (避免 TaskPulse 表无限膨胀)
    2. 识别并标记僵尸 Worker (心跳超时)
    """
    now = timezone.now()
    
    # --- 1. 僵尸节点清理 ---
    # 如果超过 2 分钟未收到心跳，强制标记为离线
    offline_threshold = now - timedelta(minutes=2)
    zombie_workers = WorkerNode.objects.filter(
        status='online', 
        last_heartbeat__lt=offline_threshold
    )
    zombie_count = zombie_workers.update(status='offline')
    if zombie_count > 0:
        logger.warning(f"[TaskPulse] 发现 {zombie_count} 个僵尸 Worker，已将其状态标记为 offline。")

    # --- 2. 历史数据清理 ---
    # 清理 7 天前的任务执行记录
    retention_days = 7
    data_retention_threshold = now - timedelta(days=retention_days)
    old_tasks = TaskPulse.objects.filter(create_time__lt=data_retention_threshold)
    
    deleted_count, _ = old_tasks.delete()
    if deleted_count > 0:
        logger.info(f"[TaskPulse] 数据清理：已删除 {deleted_count} 条超过 {retention_days} 天的旧任务记录。")

    return {
        "zombie_workers_marked": zombie_count,
        "old_tasks_deleted": deleted_count
    }
