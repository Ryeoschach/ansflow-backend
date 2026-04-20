from django.apps import AppConfig


class ConfigCenterConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.config_center'

    def ready(self):
        # 注册配置订阅者
        from utils.config_subscribers import register_config_subscribers
        register_config_subscribers()

        # 初始化配置广播订阅者（多实例同步）
        from utils.config_broadcast import init_config_broadcast_subscriber
        init_config_broadcast_subscriber()
