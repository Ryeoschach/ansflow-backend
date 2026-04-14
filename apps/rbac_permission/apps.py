from django.apps import AppConfig


class RbacPermissionConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.rbac_permission'

    def ready(self):
        # 导入信号
        import apps.rbac_permission.signals