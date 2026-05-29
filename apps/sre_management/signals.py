from django.db.models.signals import post_save
from django.dispatch import receiver
from apps.pipeline_management.models import PipelineRun
from .models import AlertEvent

@receiver(post_save, sender=PipelineRun)
def sync_pipeline_status_to_alert(sender, instance, **kwargs):
    """
    当流水线状态变化时，同步更新关联的告警自愈状态
    """
    # 查找关联了该流水线运行实例的告警
    alerts = AlertEvent.objects.filter(latest_run_id=instance.id)
    if not alerts.exists():
        return

    new_status = 'executing'
    if instance.status == 'success':
        new_status = 'success'
    elif instance.status in ['failed', 'cancelled']:
        new_status = 'failed'
    elif instance.status == 'pending':
        new_status = 'executing'

    # 更新告警状态
    alerts.update(healing_status=new_status)

@receiver(post_save, sender=AlertEvent)
def trigger_ai_analysis_on_new_alert(sender, instance, created, **kwargs):
    """
    当新告警产生时，自动触发 AI 诊断任务
    """
    if created and instance.healing_status == 'none':
        from apps.ai_engine.tasks import analyze_alert_event_task
        # 延迟触发，确保数据库事务已提交
        from django.db import transaction
        transaction.on_commit(lambda: analyze_alert_event_task.delay(instance.id))
