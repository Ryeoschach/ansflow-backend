from django.core.management.base import BaseCommand
from config.celery import app as celery_app
from apps.task_pulse.monitor import PulseMonitor

class Command(BaseCommand):
    help = '启动 TaskPulse 实时监控器'

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS('正在启动 TaskPulse 实时监控...'))
        monitor = PulseMonitor(celery_app)
        try:
            monitor.run()
        except KeyboardInterrupt:
            self.stdout.write(self.style.WARNING('TaskPulse 监控已停止'))
