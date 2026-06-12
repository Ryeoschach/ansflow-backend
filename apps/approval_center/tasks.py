from celery import shared_task
from django.utils import timezone

from .models import ApprovalTicket


@shared_task(name='expire_approval_tickets')
def expire_approval_tickets():
    now = timezone.now()
    return ApprovalTicket.objects.filter(
        status='pending',
        expires_at__lte=now,
    ).update(
        status='canceled',
        remark='审批已超时，挂起请求已自动失效。',
        audit_time=now,
    )
