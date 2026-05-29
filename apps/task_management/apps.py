from django.apps import AppConfig


class TaskManagementConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.task_management'
    verbose_name = '任务执行管理'

    def ready(self):
        from apps.approval_center.registry import approval_registry
        approval_registry.register(
            code="ansible:execution",
            name="Ansible 任务执行 (高危指令拦截)",
            name_en="Ansible Execution (High-risk Command Intercept)",
            icon="ConsoleSqlOutlined",
            description="针对临时任务下发或批量指令执行的审批拦截。",
            description_en="Approval interception for ad-hoc task dispatch or batch command execution."
        )
