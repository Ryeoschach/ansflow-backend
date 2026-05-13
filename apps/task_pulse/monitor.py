import logging
from django.utils import timezone
from django.db import transaction
from celery import Celery
from django.conf import settings
from .models import WorkerNode, TaskPulse

logger = logging.getLogger(__name__)

class PulseMonitor:
    def __init__(self, celery_app):
        self.app = celery_app
        self.state = self.app.events.State()

    def on_worker_online(self, event):
        hostname = event.get('hostname')
        logger.info(f"Worker Online: {hostname}")
        WorkerNode.objects.update_or_create(
            hostname=hostname,
            defaults={
                'status': 'online',
                'last_heartbeat': timezone.now()
            }
        )

    def on_worker_offline(self, event):
        hostname = event.get('hostname')
        logger.info(f"Worker Offline: {hostname}")
        WorkerNode.objects.filter(hostname=hostname).update(status='offline')

    def on_worker_heartbeat(self, event):
        hostname = event.get('hostname')
        loadavg = event.get('loadavg')
        processed = event.get('processed')
        
        update_data = {
            'status': 'online',
            'last_heartbeat': timezone.now()
        }
        if loadavg: update_data['load_avg'] = loadavg
        if processed: update_data['processed_count'] = processed
        
        WorkerNode.objects.filter(hostname=hostname).update(**update_data)

    def on_task_event(self, event):
        task_id = event.get('uuid')
        state = event.get('type').replace('task-', '').upper()
        hostname = event.get('hostname')
        
        worker = None
        if hostname:
            worker, _ = WorkerNode.objects.get_or_create(hostname=hostname)

        defaults = {
            'state': state,
            'worker': worker,
        }
        
        # 基础元数据
        if 'name' in event: defaults['name'] = event['name']
        if 'args' in event: defaults['args'] = event['args']
        if 'kwargs' in event: defaults['kwargs'] = event['kwargs']
        if 'runtime' in event: defaults['runtime'] = event['runtime']
        if 'exception' in event: defaults['traceback'] = event['exception']
        if 'result' in event: defaults['result'] = event['result']
        
        # 路由与层级
        if 'routing_key' in event: defaults['routing_key'] = event['routing_key']
        if 'exchange' in event: defaults['exchange'] = event['exchange']
        if 'parent_id' in event: defaults['parent_id'] = event['parent_id']

        if 'timestamp' in event:
            dt = timezone.datetime.fromtimestamp(event['timestamp'], tz=timezone.get_current_timezone())
            if state == 'STARTED':
                defaults['start_time'] = dt
            elif state in ['SUCCESS', 'FAILURE', 'REVOKED']:
                defaults['end_time'] = dt

        with transaction.atomic():
            TaskPulse.objects.update_or_create(
                task_id=task_id,
                defaults=defaults
            )

    def run(self):
        logger.info("Starting TaskPulse Monitor...")
        
        # 启动时主动发现已在线的 Worker
        try:
            pings = self.app.control.ping(timeout=1.0)
            for ping in pings:
                for hostname in ping:
                    logger.info(f"Detected existing worker: {hostname}")
                    WorkerNode.objects.update_or_create(
                        hostname=hostname,
                        defaults={'status': 'online', 'last_heartbeat': timezone.now()}
                    )
        except Exception as e:
            logger.warning(f"Initial worker detection failed: {e}")

        with self.app.connection() as connection:
            recv = self.app.events.Receiver(connection, handlers={
                'worker-online': self.on_worker_online,
                'worker-offline': self.on_worker_offline,
                'worker-heartbeat': self.on_worker_heartbeat,
                'task-received': self.on_task_event,
                'task-started': self.on_task_event,
                'task-succeeded': self.on_task_event,
                'task-failed': self.on_task_event,
                'task-revoked': self.on_task_event,
                'task-retried': self.on_task_event,
            })
            recv.capture(limit=None, timeout=None, wakeup=True)
