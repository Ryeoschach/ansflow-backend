from django.apps import AppConfig


class TaskManagementConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.task_management'
    verbose_name = '任务执行管理'
