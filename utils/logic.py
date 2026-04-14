from apps.rbac_permission.models import Menu

def calculate_user_perms(user):
    """
    计算用户的所有权限码列表 (包含角色继承)
    """
    all_perms_set = set()
    # 遍历用户的所有角色
    for role in user.roles.all():
        # 调用模型中递归获取权限的方法
        role_perms = role.get_all_permissions()
        for p in role_perms:
            if p.is_active:
                all_perms_set.add(p.code)
    return list(all_perms_set)


def calculate_user_menu_tree(user):
    """
    计算用户授权的菜单树
    """
    # 避免循环导入
    from apps.rbac_permission.serializers import MenuSerializer

    is_superuser = user.is_superuser
    menu_ids = None

    if is_superuser:
        # 超级管理员获取所有顶级菜单
        queryset = Menu.objects.filter(parent__isnull=True).order_by('order')
    else:
        # 递归获取关联的所有菜单 ID (支持角色继承)
        all_menu_ids = set()
        for role in user.roles.all():
            ids = role.get_all_menus().values_list('id', flat=True)
            all_menu_ids.update(ids)
        
        directly_assigned_ids = list(all_menu_ids)

        # 溯源汇总所有父级 ID (确保树形结构的完整性)
        all_required_ids = set()
        def collect_ancestors(menu_obj):
            if menu_obj.id not in all_required_ids:
                all_required_ids.add(menu_obj.id)
                if menu_obj.parent:
                    collect_ancestors(menu_obj.parent)

        authorized_menus = Menu.objects.filter(id__in=directly_assigned_ids)
        for m in authorized_menus:
            collect_ancestors(m)

        menu_ids = list(all_required_ids)
        # 筛选属于该用户的顶级菜单
        queryset = Menu.objects.filter(id__in=menu_ids, parent__isnull=True).order_by('order')

    # 序列化生成树状结构
    serializer = MenuSerializer(
        queryset,
        many=True,
        context={
            'menu_ids': menu_ids,
            'is_superuser': is_superuser
        }
    )
    return serializer.data
