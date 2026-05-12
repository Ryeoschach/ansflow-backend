import os
import django
import logging

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.local')
django.setup()

from apps.sre_management.tasks import analyze_alert_event
from apps.sre_management.models import AlertEvent

logging.basicConfig(level=logging.INFO)

alert = AlertEvent.objects.last()
if alert:
    print(f"Testing analysis for alert: {alert.id}")
    res = analyze_alert_event(alert.id)
    print(res)
else:
    print("No alert found")
