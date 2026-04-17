"""
配置广播模块
用于多实例间配置变更的同步
"""
import json
import logging
from typing import Any, Optional
from django.conf import settings

logger = logging.getLogger(__name__)

# Redis Channel 名称
CHANNEL_NAME = 'config:center:broadcast'


class ConfigBroadcaster:
    """
    配置广播器
    使用 Redis Pub/Sub 在多实例间同步配置变更
    """

    @classmethod
    def _get_redis_connection(cls):
        """获取 Redis 连接"""
        try:
            from django_redis import get_redis_connection
            return get_redis_connection('default')
        except Exception as e:
            logger.warning(f'Failed to get Redis connection: {e}')
            return None

    @classmethod
    def broadcast(cls, category: str, key: str, value: Any):
        """
        广播配置变更到其他实例
        """
        conn = cls._get_redis_connection()
        if conn is None:
            return False

        try:
            message = json.dumps({
                'category': category,
                'key': key,
                'value': value,
            })
            conn.publish(CHANNEL_NAME, message)
            logger.info(f'Config broadcast: {category}.{key}')
            return True
        except Exception as e:
            logger.error(f'Failed to broadcast config: {e}')
            return False

    @classmethod
    def subscribe(cls, callback):
        """
        订阅配置变更（通常在应用启动时调用）
        callback: 回调函数，接收 (category, key, value) 参数
        """
        import threading
        thread = threading.Thread(target=cls._subscribe_loop, args=(callback,), daemon=True)
        thread.start()
        logger.info('Config broadcast subscriber started')

    @classmethod
    def _subscribe_loop(cls, callback):
        """订阅循环"""
        try:
            conn = cls._get_redis_connection()
            if conn is None:
                logger.warning('Cannot subscribe: Redis connection unavailable')
                return

            pubsub = conn.pubsub()
            pubsub.subscribe(CHANNEL_NAME)

            for message in pubsub.listen():
                if message['type'] == 'message':
                    try:
                        data = json.loads(message['data'])
                        callback(data['category'], data['key'], data['value'])
                    except Exception as e:
                        logger.error(f'Failed to process broadcast message: {e}')
        except Exception as e:
            logger.error(f'Subscribe loop error: {e}')


def broadcast_config_change(category: str, key: str, value: Any):
    """广播配置变更"""
    return ConfigBroadcaster.broadcast(category, key, value)


def on_config_broadcast_received(category: str, key: str, value: Any):
    """
    收到配置广播时的处理
    """
    from utils.config_manager import ConfigCache, ConfigNotifier

    logger.info(f'Received config broadcast: {category}.{key}')

    # 更新本地缓存
    ConfigCache.invalidate(category, key)

    # 通知本地订阅者
    ConfigNotifier.notify(category, key, value)


def init_config_broadcast_subscriber():
    """初始化配置广播订阅者"""
    if not getattr(settings, 'ANSFLOW_CONFIG_BROADCAST_ENABLED', True):
        logger.info('Config broadcast disabled')
        return

    try:
        ConfigBroadcaster.subscribe(on_config_broadcast_received)
        logger.info('Config broadcast subscriber initialized')
    except Exception as e:
        logger.warning(f'Failed to init config broadcast subscriber: {e}')
