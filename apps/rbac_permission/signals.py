from django.db.models.signals import m2m_changed, post_save, post_delete
from django.dispatch import receiver
from django.core.cache import cache
from .models import User, Role, Menu, Permission
from .tasks import refresh_bulk_cache_task

def dispatch_refresh(user_ids):
    """
    统一任务分发：静默刷新（异步覆盖缓存）
    """
    if not user_ids:
        return
    
    # 转换为列表并去重
    user_ids = list(set(user_ids))
    
    # 异步执行全量刷新任务，直接覆盖 Redis 里的旧值
    refresh_bulk_cache_task.apply_async(
        kwargs={'user_ids': user_ids},
        countdown=1  # 设置 1 秒延迟，给数据库事务提交留出时间
    )

# 1. 用户角色关联变动
@receiver(m2m_changed, sender=User.roles.through)
def on_user_role_change(instance, action, **kwargs):
    if action in ["post_add", "post_remove", "post_clear"]:
        dispatch_refresh([instance.id])

# 2. 角色定义的变动（权限关联、继承关联、菜单关联）
@receiver(m2m_changed, sender=Role.permissions.through)
@receiver(m2m_changed, sender=Role.parents.through)
@receiver(m2m_changed, sender=Role.menus.through)
def on_role_structure_change(instance, action, **kwargs):
    if action in ["post_add", "post_remove", "post_clear"]:
        # 直接/间接拥有该角色的角色 ID 集合
        role_ids = {instance.id}
        role_ids.update(instance.get_all_descendant_roles().values_list('id', flat=True))
        # 拿到所有拥有这些角色的用户 ID
        affected_user_ids = list(User.objects.filter(roles__id__in=role_ids).values_list('id', flat=True))
        dispatch_refresh(affected_user_ids)

# 菜单定义本身的变动（增、删、改标题或图标）
@receiver(post_save, sender=Menu)
@receiver(post_delete, sender=Menu)
def on_menu_change(sender, instance, **kwargs):
    """
    当菜单定义改变，刷新：
    1. 所有超级管理员
    2. 直接拥有该菜单的角色及其子角色关联的用户
    """
    super_uids = list(User.objects.filter(is_superuser=True).values_list('id', flat=True))
    roles_with_menu = Role.objects.filter(menus=instance)
    
    affected_role_ids = set()
    for r in roles_with_menu:
        affected_role_ids.add(r.id)
        affected_role_ids.update(r.get_all_descendant_roles().values_list('id', flat=True))
    
    affected_uids = list(User.objects.filter(roles__id__in=affected_role_ids).values_list('id', flat=True))
    dispatch_refresh(super_uids + affected_uids)

# 权限项本身的变动（如：禁用某个 code）
@receiver(post_save, sender=Permission)
@receiver(post_delete, sender=Permission)
def on_permission_change(sender, instance, **kwargs):
    super_uids = list(User.objects.filter(is_superuser=True).values_list('id', flat=True))
    roles_with_perm = Role.objects.filter(permissions=instance)
    
    affected_role_ids = set()
    for r in roles_with_perm:
        affected_role_ids.add(r.id)
        affected_role_ids.update(r.get_all_descendant_roles().values_list('id', flat=True))
    
    affected_uids = list(User.objects.filter(roles__id__in=affected_role_ids).values_list('id', flat=True))
    dispatch_refresh(super_uids + affected_uids)

# 用户基础资料变动
@receiver(post_save, sender=User)
def on_user_info_change(sender, instance, created, **kwargs):
    if not created:
        dispatch_refresh([instance.id])