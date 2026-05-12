from django.apps import AppConfig


class HostManagementConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.host_management'
    verbose_name = '主机资产管理'

    def ready(self):
        from apps.approval_center.registry import approval_registry
        approval_registry.register(
            code="host:terminal_access",
            name="SSH 终端登录 (远程访问审批)",
            icon="KeyOutlined",
            description="当用户尝试通过 Web 终端连接主机时，若命中了拦截策略，则需管理员授权后方可建立连接。"
        )
