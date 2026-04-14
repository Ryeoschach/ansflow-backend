import os
from celery import Celery
# 设置 Django 默认配置模块
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.development')
app = Celery('ansflow')
# 从 Django 的 settings 引导配置，使用 CELERY_ 前缀
app.config_from_object('django.conf:settings', namespace='CELERY')
# 自动发现每个 app 下的 tasks.py 文件
app.autodiscover_tasks()