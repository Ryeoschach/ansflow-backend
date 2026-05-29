from django.apps import AppConfig


class ApprovalCenterConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.approval_center'

    def ready(self):
        import apps.approval_center.signals
