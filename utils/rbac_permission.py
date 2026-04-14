from rest_framework import permissions
from django.core.cache import cache


# 定义全局统一的 RBAC 动作映射表
RBAC_ACTION_MAP = {
    'list': 'view',
    'get': 'view',
    'retrieve': 'view',
    'create': 'add',
    'post': 'add',
    'update': 'edit',
    'put': 'edit',
    'partial_update': 'edit',
    'patch': 'edit',
    'destroy': 'delete',
    'delete': 'delete',
}

class SmartRBACPermission(permissions.BasePermission):
    def has_permission(self, request, view):
        # 如果是超级管理员，直接通关（最高优先级）
        if request.user.is_superuser:
            return True

        # 如果未登录或者是匿名用户，直接拒掉
        if not request.user or not request.user.is_authenticated:
            return False

        # 获取资源标识
        # 如果 View 中没有定义 resource_code，说明该接口不参与 RBAC 审计，生产环境直接禁用
        resource = getattr(view, 'resource_code', None)
        if not resource:
            # 默认拒掉，除非 View 显式声明 resource_code
            return False

        # 兼容 ViewSet (view.action) 和 普通 APIView (request.method)
        # action = getattr(view, 'action', request.method.lower())
        # 获取 action：优先拿 view.action，如果为 None，则拿 request.method
        action = getattr(view, 'action', None) or request.method.lower()

        # 统一查询为view, 新加为add，编辑为edit，删除为delete，其他为自定义的action
        perm_action = RBAC_ACTION_MAP.get(action, action)

        # 提取最终存入数据库的动作标签，如果映射表里没有，就直接用原始 action 名（如：set_capital）
        # perm_action = action_map.get(action, action)

        # 如果 perm_action 是 None，给它一个最后的 fallback
        if not perm_action:
            perm_action = 'unknown'

        # 拼接出本次请求所需的权限码字符串：例如 'city:data:view'
        required_code = f"{resource}:{perm_action}"

        # 获取用户拥有的权限列表（从 Redis 缓存）
        cache_key = f"rbac:perms:user_{request.user.id}"
        user_perms_list = cache.get(cache_key)

        if user_perms_list is None:
            # ==========================================
            # 角色继承核心
            # ==========================================
            all_perms_set = set()
            # 由于使用了自定义 User 模型，直接访问 roles
            for role in request.user.roles.all():
                # 调用在 Role 模型里的递归方法
                role_perms = role.get_all_permissions()

                # 过滤掉不激活的权限，并将 code 存入集合去重
                for p in role_perms:
                    if p.is_active:
                        all_perms_set.add(p.code)

            user_perms_list = list(all_perms_set)
            # ==========================================
            timeout = 3600 if user_perms_list else 300
            cache.set(cache_key, user_perms_list, timeout)

        # 转换为 set 加速后续的 contains 判断
        user_perms = set(user_perms_list)

        # 多级匹配逻辑 (通配符逻辑)

        # 情况 A: '*'，可以执行任何处操作
        if '*' in user_perms:
            return True

        # 精确匹配
        if required_code in user_perms:
            return True


        # 情况 B: 拥有模块级或资源级通配符
        # 例如 required_code 是 'city:data:add'
        parts = required_code.split(':')  # 拆解为 ['city', 'data', 'add']

        for i in range(1, len(parts)):
            wildcard = ":".join(parts[:i]) + ":*"
            if wildcard in user_perms:
                return True

        # 权限继承：写包含读
        if perm_action == 'view':
            # 检查是否有任何同资源的“写”权限
            # 这种写法比写死数组更灵活
            has_write_perm = any(
                p.startswith(f"{resource}:") and p.split(':')[-1] in ['add', 'edit', 'delete']
                for p in user_perms
            )
            if has_write_perm:
                return True

        print(f"--- [DEBUG] 期望权限码: {required_code}")
        print(f"--- [DEBUG] 用户实际拥有的: {user_perms}")
        # 情况 C: 精确匹配判断
        return required_code in user_perms

    def has_object_permission(self, request, view, obj):
        """对象级权限校验（资源范围检查）"""
        if request.user.is_superuser:
            return True
            
        resource_type = getattr(view, 'resource_type', None)
        if not resource_type:
            return True

        # =========================================================
        # Owner 豁免：如果用户是该资源的创建者/触发者，直接通过
        # 与 DataScopeMixin 的豁免逻辑保持一致
        # =========================================================
        owner_field = getattr(view, 'resource_owner_field', None)
        if owner_field:
            owner_value = getattr(obj, owner_field, None)
            # owner_field 可能是 FK（User 对象）或直接 ID 值
            if owner_value == request.user or owner_value == request.user.id:
                return True
            
        # 根据请求动作判断所需的数据权限类型
        action = getattr(view, 'action', None) or request.method.lower()
        needed_type = 'manage' if action in ['update', 'partial_update', 'destroy', 'delete'] else 'use'
        
        allowed_ids = get_user_data_scope(request.user, resource_type, action_type=needed_type)
        if "*" in allowed_ids:
            return True

        lookup_field = getattr(view, 'resource_lookup_field', 'id')
        compare_value = getattr(obj, lookup_field, obj.id)

        try:
            return int(compare_value) in {int(i) for i in allowed_ids if str(i).isdigit()}
        except (TypeError, ValueError):
            return str(compare_value) in {str(i) for i in allowed_ids}


def get_user_data_scope(user, resource_type, action_type='use'):
    """
    获取用户对特定资源类型的授权 ID 集合
    action_type: 
        'use'    - 包含 manage 和 use (只要有管理权就一定能用)
        'manage' - 仅包含 manage
    """
    if user.is_superuser:
        return {"*"}
        
    cache_key = f"rbac:data_scope:user_{user.id}:{resource_type}:{action_type}"
    cached_ids = cache.get(cache_key)
    if cached_ids is not None:
        return set(cached_ids)
        
    all_ids = set()
    # 只要满足条件的 DataPolicy 都在采纳范围内
    target_action_types = ['manage']
    if action_type == 'use':
        target_action_types.append('use')
        
    for role in user.roles.all():
        # 获取角色继承链条上的所有策略元数据
        policies = role.get_all_data_policies()
        
        # 遍历所有被允许的 action 环境
        for atype in target_action_types:
            # 这里的 policies[rtype] 之前是 set(ids)，现在需要按 atype 分隔
            # 修改 Role.get_all_data_policies 后，它返回 { rtype: { atype: set(ids) } }
            # 为了保持兼容和简单，从模型查角色相关的 Policy
            pass 

    from apps.rbac_permission.models import DataPolicy, Role
    
    # 找到所有相关角色 (含继承父角色)
    all_role_ids = set()
    for r in user.roles.all():
        all_role_ids.add(r.id)
        # 获取父级 ID
        all_role_ids.update(r._get_all_ids('parents'))
        
    policies = DataPolicy.objects.filter(
        role_id__in=all_role_ids, 
        resource_type=resource_type,
        action_type__in=target_action_types
    )
    
    for p in policies:
        ids = p.authorized_ids
        if "*" in ids:
            all_ids = {"*"}
            break
        all_ids.update(ids)
            
    cache.set(cache_key, list(all_ids), 3600)
    return all_ids


class DataScopeMixin:
    """
    用于 ViewSet 的 Mixin，自动根据数据权限过滤 QuerySet
    要求 ViewSet 定义：
    1. resource_type: 对应 DataPolicy 中的资源类型标识 (如 'pipeline')
    2. resource_lookup_field: 过滤字段名，默认为 'id'
    3. resource_owner_field: 所有者字段名 (如 'creator' 或 'trigger_user')，如果匹配该字段则无视 ID 过滤
    """
    def get_queryset(self):
        queryset = super().get_queryset()
        user = self.request.user
        
        if not user or not user.is_authenticated:
            return queryset.none()
            
        if user.is_superuser:
            return queryset
            
        resource_type = getattr(self, 'resource_type', None)
        lookup_field = getattr(self, 'resource_lookup_field', 'id')
        owner_field = getattr(self, 'resource_owner_field', None)
        
        if not resource_type:
            # 如果没定义 resource_type 但定义了 owner_field，则仅返回自己的
            if owner_field:
                return queryset.filter(**{owner_field: user})
            return queryset
            
        # 列表查询和普通访问统一要求最低额度的 'use' 权限
        allowed_ids = get_user_data_scope(user, resource_type, action_type='use')
        
        if "*" in allowed_ids:
            return queryset
            
        # 构造组合查询：(在授权 ID 内) | (我是所有者)
        from django.db.models import Q
        filter_q = Q(**{f"{lookup_field}__in": allowed_ids})
        if owner_field:
            filter_q |= Q(**{owner_field: user})
            
        return queryset.filter(filter_q).distinct()
