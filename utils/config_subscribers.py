"""
配置订阅者实现
当配置变更时自动重新加载相关组件
"""
import logging
from typing import Any
from django.core.cache import cache
from utils.config_manager import ConfigSubscriber, ConfigNotifier

logger = logging.getLogger(__name__)


class RedisConfigSubscriber(ConfigSubscriber):
    """
    Redis 配置订阅者
    当 Redis 配置变更时，重新配置缓存连接
    """
    name = 'redis_config_subscriber'
    categories = ['redis']

    def on_config_changed(self, category: str, key: str, value: Any):
        """Redis 配置变更处理"""
        logger.info(f'Redis config changed: {category}.{key}')

        if key in ['host', 'port', 'db']:
            # 清除 Redis 连接缓存，强制重新连接
            cache.delete('redis_connection_info')
            logger.info('Redis connection cache cleared')


class LoggingConfigSubscriber(ConfigSubscriber):
    """
    日志配置订阅者
    当日志配置变更时，重新配置日志级别
    """
    name = 'logging_config_subscriber'
    categories = ['logging']

    def on_config_changed(self, category: str, key: str, value: Any):
        """日志配置变更处理"""
        import logging
        logger.info(f'Logging config changed: {category}.{key}')

        if key == 'level':
            # 动态修改日志级别
            from django.conf import settings
            if hasattr(settings, 'LOG_LEVEL'):
                # 只影响本进程（无法影响其他进程）
                logging.getLogger().setLevel(value)
                logger.info(f'Log level changed to {value}')


class CacheConfigSubscriber(ConfigSubscriber):
    """
    缓存配置订阅者
    当缓存配置变更时，清除相关缓存
    """
    name = 'cache_config_subscriber'
    categories = ['cache']

    def on_config_changed(self, category: str, key: str, value: Any):
        """缓存配置变更处理"""
        logger.info(f'Cache config changed: {category}.{key}')

        if key == 'ttl' or key == 'enabled':
            # 清除所有缓存
            cache.clear()
            logger.info('All cache cleared due to config change')


class NotificationConfigSubscriber(ConfigSubscriber):
    """
    通知配置订阅者
    当通知配置变更时，清除通知配置的缓存
    """
    name = 'notification_config_subscriber'
    categories = ['notification']

    def on_config_changed(self, category: str, key: str, value: Any):
        """通知配置变更处理"""
        from utils.config_manager import ConfigCache
        ConfigCache.invalidate('notification', key)
        logger.info(f'Notification config invalidated: notification.{key}')


def register_config_subscribers():
    """注册所有订阅者"""
    subscribers = [
        RedisConfigSubscriber(),
        LoggingConfigSubscriber(),
        CacheConfigSubscriber(),
        NotificationConfigSubscriber(),
    ]

    for subscriber in subscribers:
        ConfigNotifier.subscribe(subscriber)

    logger.info(f'Registered {len(subscribers)} config subscribers')
