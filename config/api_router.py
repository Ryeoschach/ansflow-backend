from django.urls import path, include
from rest_framework.routers import DefaultRouter

from apps.k8s_management.views import K8sClusterViewSet
from apps.rbac_permission.views import UserViewSet, RoleViewSet, PermissionViewSet, MenuViewSet, AuditLogViewSet
from apps.host_management.views import HostViewSet, EnvironmentViewSet, ResourcePoolViewSet, PlatformViewSet, SshCredentialViewSet
from apps.task_management.views import AnsibleTaskViewSet, AnsibleExecutionViewSet
from apps.pipeline_management.views import PipelineViewSet, PipelineRunViewSet, CIEnvironmentViewSet
from apps.registry_management.views import ImageRegistryViewSet
from apps.system_management.views import SystemHealthViewSet, DashboardViewSet
from apps.approval_center.views import ApprovalPolicyViewSet, ApprovalTicketViewSet
from apps.credentials_management.views import CredentialViewSet
from utils.auth_views import CookieTokenObtainPairView, CookieTokenRefreshView

router = DefaultRouter()
router.register(r'users', UserViewSet, basename='users')
router.register(r'roles', RoleViewSet, basename='roles')
router.register(r'hosts', HostViewSet, basename='hosts')
router.register(r'environments', EnvironmentViewSet, basename='environments')
router.register(r'platforms', PlatformViewSet, basename='platforms')
router.register(r'resource_pools', ResourcePoolViewSet, basename='resource_pools')
router.register(r'ssh_credentials', SshCredentialViewSet, basename='ssh_credentials')
router.register(r'system/menus', MenuViewSet, basename='system-menus')
router.register(r'system/permissions', PermissionViewSet, basename='system-permissions')
router.register(r'tasks', AnsibleTaskViewSet, basename='ansible_tasks')
router.register(r'executions', AnsibleExecutionViewSet, basename='ansible_executions')
router.register(r'k8s', K8sClusterViewSet, basename='k8s_management')
router.register(r'pipelines', PipelineViewSet, basename='pipelines')
router.register(r'pipeline_runs', PipelineRunViewSet, basename='pipeline_runs')
router.register(r'ci_environments', CIEnvironmentViewSet, basename='ci_environments')
router.register(r'image_registries', ImageRegistryViewSet, basename='image_registries')
router.register(r'system/health', SystemHealthViewSet, basename='system-health')
router.register(r'system/dashboard', DashboardViewSet, basename='system-dashboard')
router.register(r'audit-logs', AuditLogViewSet, basename='审计日志')
router.register(r'credentials', CredentialViewSet, basename='credentials')

app_router = DefaultRouter()
router.register(r'approval_policies', ApprovalPolicyViewSet, basename='approval_policies')
router.register(r'approval_tickets', ApprovalTicketViewSet, basename='approval_tickets')



# 2. 拼接扁平化逻辑
api_v1_patterns = [
    # 认证逻辑 (Auth)
    path('auth/login/', CookieTokenObtainPairView.as_view(), name='login'),
    path('auth/refresh/', CookieTokenRefreshView.as_view(), name='refresh'),

    # 账号逻辑 (Account) —— 指向 ViewSet 里的特定 Action
    path('account/me/', UserViewSet.as_view({'get': 'me'}), name='account-me'),
    path('account/menus/', MenuViewSet.as_view({'get': 'my_menus'}), name='account-menus'),

    # 将标准 Router 里的路径挂载进来
    path('', include(router.urls)),
]
