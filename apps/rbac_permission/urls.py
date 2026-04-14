from rest_framework import routers
from django.urls import path, include

from apps.rbac_permission.views import UserViewSet, RoleViewSet, PermissionViewSet, MenuViewSet, AuditLogViewSet

router = routers.DefaultRouter()
router.register(r'users', UserViewSet, basename='用户')
router.register(r'roles', RoleViewSet, basename='角色')
router.register(r'permissions', PermissionViewSet, basename='权限')
router.register(r'menu', MenuViewSet, basename='菜单树')
router.register(r'audit-logs', AuditLogViewSet, basename='审计日志')

urlpatterns = [
    path('', include(router.urls)),
]