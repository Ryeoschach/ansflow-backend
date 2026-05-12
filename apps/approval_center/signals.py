from django.db.models.signals import post_save
from django.dispatch import receiver
from .models import ApprovalTicket
import logging

logger = logging.getLogger(__name__)

@receiver(post_save, sender=ApprovalTicket)
def on_approval_ticket_update(sender, instance, created, **kwargs):
    """
    监听审批工单状态变化，联动更新关联资源（如告警自愈状态）
    """
    if created:
        return

    # 仅处理终态逻辑（批准/驳回）
    if instance.status not in ['approved', 'rejected', 'finished', 'failed']:
        return

    # 从 payload 中提取关联信息
    payload = instance.payload or {}
    alert_id = payload.get('alert_id')
    
    if not alert_id:
        return

    from apps.sre_management.models import AlertEvent
    try:
        alert = AlertEvent.objects.get(id=alert_id)
        
        if instance.status == 'rejected':
            alert.healing_status = 'failed'
            alert.ai_analysis = (alert.ai_analysis or "") + f"\n\n[审批驳回] 审批人: {instance.approver.username if instance.approver else 'Unknown'}。意见: {instance.remark}"
            alert.save()
            logger.info(f"Alert {alert_id} healing status updated to failed due to approval rejection.")
        
        elif instance.status in ['approved', 'finished']:
            # 状态改为正在执行
            alert.healing_status = 'executing'
            
            # 特殊逻辑：尝试从最近一次 Proxy 执行的响应结果中提取 run_id
            # 注意：Proxy 执行后的结果通常需要从一个地方暂存或者通过上下文获取
            # 这里的 instance 已经保存了最新的状态。
            # 如果是流水线执行，payload 里通常有 pipeline_id。
            
            # 我们直接在 AlertEvent 的 ai_analysis 中记录审批通过的信息
            alert.ai_analysis = (alert.ai_analysis or "") + f"\n\n[审批通过] 审批人: {instance.approver.username if instance.approver else 'System'}。操作已放行。"
            alert.save()
            logger.info(f"Alert {alert_id} healing status updated to executing after approval.")
            
    except AlertEvent.DoesNotExist:
        logger.warning(f"Associated AlertEvent {alert_id} not found for ApprovalTicket {instance.id}")
    except Exception as e:
        logger.error(f"Error updating alert status from approval ticket: {str(e)}")
