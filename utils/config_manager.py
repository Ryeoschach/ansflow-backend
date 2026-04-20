"""
配置管理模块
提供配置的读取、缓存和热更新功能
"""
import logging
from typing import Any, Dict, Optional
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

# 缓存 key 前缀
CACHE_KEY_PREFIX = 'config:center:'
CACHE_KEY_ALL_ITEMS = f'{CACHE_KEY_PREFIX}all_items'
CACHE_KEY_CATEGORY_PREFIX = f'{CACHE_KEY_PREFIX}category:'
CACHE_KEY_ITEM_PREFIX = f'{CACHE_KEY_PREFIX}item:'
CACHE_TIMEOUT = 3600  # 1小时


class ConfigCache:
    """
    配置缓存管理器
    支持内存缓存和 Redis 分布式缓存
    """

    @staticmethod
    def _get_cache_key(category: str, key: str) -> str:
        return f'{CACHE_KEY_PREFIX}{category}:{key}'

    @classmethod
    def get(cls, category: str, key: str, default: Any = None) -> Any:
        """
        获取单个配置项
        """
        cache_key = cls._get_cache_key(category, key)
        value = cache.get(cache_key)
        if value is not None:
            return value

        # 缓存未命中，从数据库加载
        from apps.config_center.models import ConfigItem
        try:
            item = ConfigItem.objects.get(
                category__name=category,
                key=key,
                is_active=True
            )
            value = item.get_value()
            cache.set(cache_key, value, CACHE_TIMEOUT)
            return value
        except ConfigItem.DoesNotExist:
            return default

    @classmethod
    def get_category_items(cls, category_name: str) -> Dict[str, Any]:
        """
        获取指定分类下的所有配置项
        """
        cache_key = f'{CACHE_KEY_CATEGORY_PREFIX}{category_name}'
        items = cache.get(cache_key)
        if items is not None:
            return items

        from apps.config_center.models import ConfigItem, ConfigCategory
        try:
            category = ConfigCategory.objects.get(name=category_name)
            items = {
                item.key: item.get_value()
                for item in ConfigItem.objects.filter(category=category, is_active=True)
            }
            cache.set(cache_key, items, CACHE_TIMEOUT)
            return items
        except ConfigCategory.DoesNotExist:
            return {}

    @classmethod
    def get_all_configs(cls) -> Dict[str, Dict[str, Any]]:
        """
        获取所有配置（按分类组织）
        """
        cached = cache.get(CACHE_KEY_ALL_ITEMS)
        if cached is not None:
            return cached

        from apps.config_center.models import ConfigCategory, ConfigItem
        result = {}
        for category in ConfigCategory.objects.all():
            result[category.name] = {
                item.key: item.get_value()
                for item in ConfigItem.objects.filter(category=category, is_active=True)
            }
        cache.set(CACHE_KEY_ALL_ITEMS, result, CACHE_TIMEOUT)
        return result

    @classmethod
    def set(cls, category: str, key: str, value: Any):
        """
        设置配置项（同时更新缓存和数据库）
        """
        from apps.config_center.models import ConfigItem
        try:
            item = ConfigItem.objects.get(
                category__name=category,
                key=key,
                is_active=True
            )
            item.set_value(value)
            item.save()

            # 更新缓存
            cache.set(cls._get_cache_key(category, key), item.get_value(), CACHE_TIMEOUT)
            # 清除分类缓存
            cache.delete(f'{CACHE_KEY_CATEGORY_PREFIX}{category}')
            cache.delete(CACHE_KEY_ALL_ITEMS)

            logger.info(f'Config updated: {category}.{key} = {value}')
        except ConfigItem.DoesNotExist:
            logger.warning(f'Config item not found: {category}.{key}')

    @classmethod
    def invalidate(cls, category: str, key: str):
        """
        清除指定配置的缓存
        """
        cache.delete(cls._get_cache_key(category, key))
        cache.delete(f'{CACHE_KEY_CATEGORY_PREFIX}{category}')
        cache.delete(CACHE_KEY_ALL_ITEMS)

    @classmethod
    def invalidate_all(cls):
        """
        清除所有配置缓存
        """
        # 注意：这里无法清除所有 Redis keys，所以依赖 TTL 过期
        # 生产环境建议使用 Redis 的 SCAN + DEL 或版本号机制
        cache.delete(CACHE_KEY_ALL_ITEMS)
        logger.info('All config cache invalidated')


class ConfigSubscriber:
    """
    配置变更订阅者基类
    子类继承并实现 on_config_changed 方法
    """
    name: str = 'unknown'
    categories: list = []  # 关注的分类列表，空列表表示关注所有

    def on_config_changed(self, category: str, key: str, value: Any):
        """
        配置变更回调
        """
        raise NotImplementedError

    def should_handle(self, category: str) -> bool:
        """是否应该处理此分类的配置变更"""
        return len(self.categories) == 0 or category in self.categories


class ConfigNotifier:
    """
    配置变更通知器
    管理订阅者并在配置变更时通知
    """
    _subscribers: list = []

    @classmethod
    def subscribe(cls, subscriber: ConfigSubscriber):
        """注册订阅者"""
        if subscriber not in cls._subscribers:
            cls._subscribers.append(subscriber)
            logger.debug(f'Subscriber registered: {subscriber.name}')

    @classmethod
    def unsubscribe(cls, subscriber: ConfigSubscriber):
        """取消订阅"""
        if subscriber in cls._subscribers:
            cls._subscribers.remove(subscriber)

    @classmethod
    def notify(cls, category: str, key: str, value: Any):
        """通知所有订阅者配置变更"""
        for subscriber in cls._subscribers:
            try:
                if subscriber.should_handle(category):
                    subscriber.on_config_changed(category, key, value)
            except Exception as e:
                logger.error(f'Subscriber {subscriber.name} error: {e}')


# 便捷函数
def get_config(category: str, key: str, default: Any = None) -> Any:
    """获取配置"""
    return ConfigCache.get(category, key, default)


def get_category_config(category: str) -> Dict[str, Any]:
    """获取分类下所有配置"""
    return ConfigCache.get_category_items(category)


def set_config(category: str, key: str, value: Any):
    """设置配置"""
    ConfigCache.set(category, key, value)


def subscribe(subscriber: ConfigSubscriber):
    """注册订阅者"""
    ConfigNotifier.subscribe(subscriber)
