from rest_framework import viewsets, permissions, filters
from rest_framework.response import Response
from rest_framework.decorators import action
from apps.rbac_permission.models import User, Role, Permission, Menu, AuditLog
from apps.rbac_permission.serializers import UserSerializer, RoleSerializer, PermissionSerializer, MenuSerializer, AuditLogSerializer
from utils.rbac_permission import SmartRBACPermission
from django.core.cache import cache

from utils.logic import calculate_user_menu_tree


class UserViewSet(viewsets.ModelViewSet):
    """
    用戶管理：

    """
    queryset = User.objects.all()
    serializer_class = UserSerializer
    permission_classes = [SmartRBACPermission]

    # 开启过滤（显式指定可以过滤的字段）
    filterset_fields = ['is_active', 'is_superuser']  # 精确匹配字段

    # 开启关键词搜索（对应 ?search=admin）
    search_fields = ['username', 'email', 'mobile']

    # 开启排序（对应 ?ordering=-id）
    ordering_fields = ['id', 'date_joined']

    resource_code = 'rbac:user'

    def get_permissions(self):
        # me 接口应该允许任何已登录用户访问，不需要 rbac:user 权限
        if self.action == 'me':
            return [permissions.IsAuthenticated()]
        return super().get_permissions()

    @action(detail=False, methods=['get'])
    def me(self, request):
        user = request.user
        
        # 如果是超级管理员，直接赋予所有权限标识
        if user.is_superuser:
            perms = ['*']
        else:
            # 增加缓存逻辑，支持“静默刷新”
            cache_key = f"rbac:perms:user_{user.id}"
            perms = cache.get(cache_key)
            if perms is None:
                from utils.logic import calculate_user_perms
                perms = calculate_user_perms(user)
                # 写入缓存，1小时有效期
                cache.set(cache_key, perms, timeout=3600)

        return Response({
            "username": user.username,
            "roles": [r.name for r in user.roles.all()],
            "permissions": perms,  # 给前端做按钮权限控制
            "is_superuser": user.is_superuser
        })

    @action(detail=True, methods=['post'])
    def assign_roles(self, request, pk=None):
        """
        手动分配角色
        """
        user = self.get_object()
        role_ids = request.data.get('role_ids', [])
        
        # 将 role_ids 设置为用户角色
        user.roles.set(role_ids)
        user.save()
        
        return Response({"message": "角色分配成功"})


class RoleViewSet(viewsets.ModelViewSet):
    queryset = Role.objects.all()
    serializer_class = RoleSerializer
    permission_classes = [SmartRBACPermission]

    resource_code = 'rbac:role'

    def update(self, request, *args, **kwargs):
        # 允许部分更新
        kwargs['partial'] = True
        return super().update(request, *args, **kwargs)

    @action(detail=True, methods=['post'])
    def update_data_policies(self, request, pk=None):
        """
        更新角色下的资源级授权策略
        支持格式 A (扁平): { 'pipeline': [1,2] } -> 默认为 manage
        支持格式 B (分动作): { 'pipeline': { 'manage': [1,2], 'use': [3,4] } }
        """
        role = self.get_object()
        data = request.data
        
        from .models import DataPolicy
        
        for rtype, config in data.items():
            if isinstance(config, list):
                # 兼容旧格式，视为 manage
                policy, _ = DataPolicy.objects.get_or_create(role=role, resource_type=rtype, action_type='manage')
                policy.authorized_ids = config
                policy.save()
            elif isinstance(config, dict):
                # 新格式：指定动作
                for atype, ids in config.items():
                    if not isinstance(ids, list): continue
                    policy, _ = DataPolicy.objects.get_or_create(role=role, resource_type=rtype, action_type=atype)
                    policy.authorized_ids = ids
                    policy.save()
            
        # 任务分发：刷新受影响的人
        from .signals import dispatch_refresh
        affected_roles = Role.objects.filter(id=role.id) | role.get_all_descendant_roles()
        affected_user_ids = list(User.objects.filter(roles__in=affected_roles).values_list('id', flat=True))
        dispatch_refresh(affected_user_ids)
        
        return Response({"message": "多维度数据权限策略同步完成"})


class PermissionViewSet(viewsets.ModelViewSet):
    # queryset = Permission.objects.all()
    queryset = Permission.objects.filter(is_active=True)
    serializer_class = PermissionSerializer
    permission_classes = [SmartRBACPermission]

    resource_code = 'rbac:permission'


class MenuViewSet(viewsets.ModelViewSet):
    """
    菜单管理视图集：支持树形显示和标准管理
    """
    queryset = Menu.objects.all().order_by('order')
    serializer_class = MenuSerializer
    pagination_class = None
    permission_classes = [SmartRBACPermission]

    resource_code = 'rbac:menu'

    filter_backends = [filters.SearchFilter]
    search_fields = ['title', 'key']

    def get_permissions(self):
        """
        权限动态配置：
        获取个人菜单接口 (my_menus) 应该允许所有登录用户访问，
        而其他的 CRUD 接口则需要满足 rbac:menu 权限标识。
        """
        if self.action == 'my_menus':
            return [permissions.IsAuthenticated()]
        return super().get_permissions()

    def get_queryset(self):
        """
        增强检索逻辑：
        如果查询参数包含 parent_is_null=true，则只拉取顶级菜单。
        结合 MenuSerializer 的递归机制，可以实现一次性获取完整的菜单树。
        """
        queryset = super().get_queryset()
        parent_is_null = self.request.query_params.get('parent_is_null')
        if parent_is_null == 'true':
            # 只返回没有父级的菜单（即根节点）
            return queryset.filter(parent__isnull=True)
        return queryset

    @action(detail=False, methods=['get'])
    def my_menus(self, request):
        user = request.user
        cache_key = f"rbac:menus:user_{user.id}"

        # 1. 尝试从缓存获取
        cached_menus = cache.get(cache_key)
        if cached_menus:
            return Response(cached_menus)
        # 2. 调用公共逻辑计算
        data = calculate_user_menu_tree(user)

        # 3. 回写缓存
        cache.set(cache_key, data, timeout=3600)

        return Response(data)


class AuditLogViewSet(viewsets.ReadOnlyModelViewSet):
    """
    审计日志控制层：只读视口，支持多维全量搜索与过滤
    """
    queryset = AuditLog.objects.all().order_by('-create_time')
    serializer_class = AuditLogSerializer
    permission_classes = [SmartRBACPermission]
    resource_code = 'rbac:audit'

    # Filter fields
    filterset_fields = ['username', 'method', 'response_status', 'action', 'resource']
    
    # Search fields
    from django_filters.rest_framework import DjangoFilterBackend
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['username', 'path', 'resource_name', 'action_name', 'object_id']
    
    # Ordering
    ordering_fields = ['create_time', 'duration', 'response_status']