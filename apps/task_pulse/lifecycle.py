import logging
from datetime import timedelta

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from .models import WorkerNode

logger = logging.getLogger(__name__)


def get_worker_heartbeat_timeout() -> timedelta:
    seconds = getattr(settings, 'TASK_PULSE_WORKER_HEARTBEAT_TIMEOUT_SECONDS', 120)
    return timedelta(seconds=max(int(seconds), 1))


def get_offline_worker_retention() -> timedelta | None:
    hours = getattr(settings, 'TASK_PULSE_OFFLINE_WORKER_RETENTION_HOURS', 24)
    hours = int(hours)
    if hours <= 0:
        return None
    return timedelta(hours=hours)


def mark_stale_workers_offline(now=None) -> int:
    now = now or timezone.now()
    offline_threshold = now - get_worker_heartbeat_timeout()
    stale_workers = WorkerNode.objects.filter(status='online').filter(
        Q(last_heartbeat__lt=offline_threshold)
        | Q(last_heartbeat__isnull=True, create_time__lt=offline_threshold)
    )
    stale_count = stale_workers.update(status='offline')
    if stale_count:
        logger.warning("[TaskPulse] Marked %s stale worker(s) offline.", stale_count)
    return stale_count


def prune_expired_offline_workers(now=None) -> int:
    retention = get_offline_worker_retention()
    if retention is None:
        return 0

    now = now or timezone.now()
    prune_threshold = now - retention
    expired_workers = WorkerNode.objects.filter(status='offline').filter(
        Q(last_heartbeat__lt=prune_threshold)
        | Q(last_heartbeat__isnull=True, create_time__lt=prune_threshold)
    )
    deleted_count, _ = expired_workers.delete()
    if deleted_count:
        logger.info("[TaskPulse] Pruned %s expired offline worker(s).", deleted_count)
    return deleted_count


def refresh_worker_lifecycle(now=None) -> dict:
    now = now or timezone.now()
    return {
        'stale_workers_marked': mark_stale_workers_offline(now),
        'expired_offline_workers_deleted': prune_expired_offline_workers(now),
    }
