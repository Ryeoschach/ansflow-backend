import logging
from celery import shared_task
from django.core.cache import cache
from .models import User
from utils.logic import calculate_user_perms, calculate_user_menu_tree

logger = logging.getLogger(__name__)


@shared_task(name="rbac.refresh_user_cache_chunk")
def refresh_user_cache_chunk(user_ids):
    """
    具体的刷缓存子任务：负责一小批用户（如 500 人）
    """
    # 一次性预取用户及其角色，减少循环内的 SQL
    users = User.objects.filter(id__in=user_ids).prefetch_related('roles__permissions', 'roles__menus')

    for user in users:
        # 计算并覆盖缓存
        perms = calculate_user_perms(user)
        cache.set(f"rbac:perms:user_{user.id}", perms, 3600)

        menus = calculate_user_menu_tree(user)
        cache.set(f"rbac:menus:user_{user.id}", menus, 3600)

        # 清除资源隔离数据范围缓存，防止权限更新滞后
        resource_types = ['pipeline', 'ansible_task', 'k8s_cluster', 'resource_pool', 'registry', 'credential']
        action_types = ['manage', 'use']
        for rtype in resource_types:
            for atype in action_types:
                cache.delete(f"rbac:data_scope:user_{user.id}:{rtype}:{atype}")


@shared_task(name="rbac.refresh_bulk_cache_task")
def refresh_bulk_cache_task(user_ids):
    """
    任务调度器：负责切片
    """
    CHUNK_SIZE = 500  # 每组 500 人
    # 将 10000 人切成 20 组，发送 20 个并行子任务
    for i in range(0, len(user_ids), CHUNK_SIZE):
        chunk = user_ids[i:i + CHUNK_SIZE]
        refresh_user_cache_chunk.delay(chunk)