from django.test import TestCase
from django.core.cache import cache
from apps.rbac_permission.models import User, Role, Permission, DataPolicy
from utils.rbac_permission import SmartRBACPermission, DataScopeMixin, get_user_data_scope
from rest_framework.test import APIRequestFactory
from rest_framework.views import APIView
from rest_framework import viewsets
from django.db import models
from unittest.mock import MagicMock

# 模拟一个受保护的 ViewSet
class MockModel(models.Model):
    name = models.CharField(max_length=100)
    creator = models.ForeignKey(User, on_delete=models.CASCADE, null=True)
    class Meta:
        managed = False # 不创建真实表

class MockViewSet(DataScopeMixin, viewsets.ModelViewSet):
    resource_code = 'test:resource'
    resource_type = 'test_type'
    resource_owner_field = 'creator'
    queryset = MockModel.objects.none()

class SmartRBACTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.factory = APIRequestFactory()
        self.user = User.objects.create_user(username='testuser', password='password')
        
        # 创建基础权限
        self.perm_view = Permission.objects.create(name='View', code='test:resource:view', module='test')
        self.perm_add = Permission.objects.create(name='Add', code='test:resource:add', module='test')
        self.perm_edit = Permission.objects.create(name='Edit', code='test:resource:edit', module='test')
        
        # 创建角色
        self.role_viewer = Role.objects.create(name='Viewer', code='viewer')
        self.role_viewer.permissions.add(self.perm_view)
        
        self.role_editor = Role.objects.create(name='Editor', code='editor')
        self.role_editor.permissions.add(self.perm_edit)
        # Editor 继承 Viewer
        self.role_editor.parents.add(self.role_viewer)

    def test_functional_permission_basic(self):
        """测试基础功能权限与继承"""
        self.user.roles.add(self.role_viewer)
        
        request = self.factory.get('/api/v1/mock/')
        request.user = self.user
        view = MockViewSet()
        view.action = 'list'
        
        permission = SmartRBACPermission()
        
        # 1. 拥有 view 权限，访问 list 应该通过
        self.assertTrue(permission.has_permission(request, view))
        
        # 2. 访问 create 应该拒绝
        view.action = 'create'
        self.assertFalse(permission.has_permission(request, view))

    def test_permission_inheritance_write_implies_read(self):
        """测试‘写’权限自动包含‘读’权限"""
        self.user.roles.add(self.role_editor) # 只给了 edit 权限
        
        request = self.factory.get('/api/v1/mock/')
        request.user = self.user
        view = MockViewSet()
        view.action = 'list'
        
        permission = SmartRBACPermission()
        # 拥有 edit 权限，即使没有显式分配 view，访问 list 也应通过
        self.assertTrue(permission.has_permission(request, view))

    def test_wildcard_permission(self):
        """测试通配符权限"""
        role_admin = Role.objects.create(name='Admin', code='admin')
        perm_all = Permission.objects.create(name='All', code='test:resource:*', module='test')
        role_admin.permissions.add(perm_all)
        self.user.roles.add(role_admin)
        cache.clear()  # 确保角色添加后缓存失效
        
        request = self.factory.post('/api/v1/mock/')
        request.user = self.user
        view = MockViewSet()
        view.action = 'create'
        
        permission = SmartRBACPermission()
        # 拥有 test:resource:*，访问任意 action 应通过
        self.assertTrue(permission.has_permission(request, view))

    def test_super_user_bypass(self):
        """测试超级管理员绕过所有检查"""
        self.user.is_superuser = True
        self.user.save()
        
        request = self.factory.post('/api/v1/mock/')
        request.user = self.user
        view = MockViewSet()
        view.action = 'arbitrary_action'
        
        permission = SmartRBACPermission()
        self.assertTrue(permission.has_permission(request, view))

    def test_data_scope_filtering(self):
        """测试数据权限范围过滤"""
        # 为角色分配特定数据策略
        policy = DataPolicy.objects.create(
            role=self.role_viewer,
            resource_type='test_type',
            action_type='use',
            authorized_ids=[10, 20]
        )
        self.user.roles.add(self.role_viewer)
        
        # 验证获取到的授权 ID
        allowed_ids = get_user_data_scope(self.user, 'test_type', action_type='use')
        self.assertEqual(allowed_ids, {10, 20})
        
        # 验证通配符数据策略
        policy.authorized_ids = ["*"]
        policy.save()
        cache.clear()
        allowed_ids = get_user_data_scope(self.user, 'test_type', action_type='use')
        self.assertEqual(allowed_ids, {"*"})

    def test_data_scope_owner_exemption(self):
        """测试数据权限中的所有者豁免"""
        # 用户没有任何显式数据策略
        self.user.roles.add(self.role_viewer)
        
        # 模拟一个对象，用户是 creator
        mock_obj = MagicMock()
        mock_obj.id = 99
        mock_obj.creator = self.user
        
        view = MockViewSet()
        view.action = 'retrieve'
        request = self.factory.get('/api/v1/mock/99/')
        request.user = self.user
        
        permission = SmartRBACPermission()
        # 即使没有 ID 99 的授权，但因为是 owner，has_object_permission 应通过
        self.assertTrue(permission.has_object_permission(request, view, mock_obj))

    def test_project_membership_permissions(self):
        """测试项目成员权限"""
        from apps.rbac_permission.models import Project, ProjectMember
        
        # 创建一个项目
        project = Project.objects.create(name='Test Project', code='test_proj')
        
        request = self.factory.get('/api/v1/mock/')
        request.user = self.user
        request.project = project
        
        view = MockViewSet()
        view.action = 'list'
        permission = SmartRBACPermission()
        
        # 1. 默认情况下，用户不是项目成员，访问被拒
        cache.clear()
        self.user.roles.add(self.role_viewer)
        with self.assertRaises(Exception) as context:
            permission.has_permission(request, view)
        self.assertIn("没有当前项目", str(context.exception))
        
        # 2. 将用户加入项目，访问应该通过
        cache.clear()
        ProjectMember.objects.create(project=project, user=self.user, role='member')
        self.assertTrue(permission.has_permission(request, view))
        
        # 3. 超级管理员不受项目限制
        request_admin = self.factory.get('/api/v1/mock/')
        admin_user = User.objects.create_superuser(username='admin_user', email='a@b.com', password='password')
        request_admin.user = admin_user
        request_admin.project = project
        self.assertTrue(permission.has_permission(request_admin, view))

    def test_project_queryset_filtering(self):
        """测试 DataScopeMixin 按当前项目过滤查询集"""
        from apps.rbac_permission.models import Project
        from apps.credentials_management.models import Credential
        from apps.credentials_management.views import CredentialViewSet
        
        # 清理已有数据
        Credential.objects.all().delete()
        
        # 创建两个项目
        project1 = Project.objects.create(name='Project 1', code='p1')
        project2 = Project.objects.create(name='Project 2', code='p2')
        
        # 每个项目各建一个 Credential
        c1 = Credential.objects.create(name='C1', type='ssh_key', secret_value='key1', project=project1)
        c2 = Credential.objects.create(name='C2', type='ssh_key', secret_value='key2', project=project2)
        
        # 模拟请求 project1
        request = self.factory.get('/api/v1/credentials/')
        request.user = self.user
        request.project = project1
        
        # 实例化视图并获取过滤后的 queryset
        view = CredentialViewSet()
        view.request = request
        view.format_kwarg = None
        
        # 由于 user 没有 superuser，我们需要给它授权或者将 user 设为 superuser 以测试项目过滤
        self.user.is_superuser = True
        self.user.save()
        
        qs = view.get_queryset()
        self.assertEqual(qs.count(), 1)
        self.assertEqual(qs.first().id, c1.id)
