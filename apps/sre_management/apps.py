from django.apps import AppConfig

class SREManagementConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.sre_management'
    verbose_name = 'SRE 运维管理'

    def ready(self):
        import apps.sre_management.signals
        try:
            import apps.sre_management.tasks
        except ImportError:
            pass
