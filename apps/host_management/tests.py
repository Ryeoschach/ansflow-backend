import json
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase
from apps.host_management.models import Environment, Host, Platform
from apps.host_management.providers.factory import ProviderFactory
from apps.host_management.providers.cloud import AliyunProvider

class HostManagementTest(APITestCase):
    def setUp(self):
        # 创建基础数据
        self.env = Environment.objects.create(name="Test Env", code="test_env", color="#ff0000")
        self.platform = Platform.objects.create(name="Test Aliyun", type="aliyun")
        
        # 假设我们需要认证，这里先创建一个超级用户（如果需要权限校验的话）
        # 目前先通过 Client 直接调用，如果设置了权限类，可能需要强制认证
        from django.contrib.auth import get_user_model
        User = get_user_model()
        self.user = User.objects.create_superuser(username='testadmin', password='password', email='test@test.com')
        self.client.force_authenticate(user=self.user)

    def test_bulk_import_success(self):
        """测试批量导入主机成功"""
        url = reverse('hosts-bulk-import')
        data = [
            {
                "hostname": "bulk-host-1",
                "private_ip": "10.0.0.1",
                "env": self.env.id,
                "cpu": 4
            },
            {
                "hostname": "bulk-host-2",
                "private_ip": "10.0.0.2",
                "env": self.env.id,
                "platform": self.platform.id
            }
        ]
        response = self.client.post(url, data, format='json')
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['status'], 'success')
        self.assertEqual(Host.objects.count(), 2)
        self.assertEqual(Host.objects.get(hostname="bulk-host-1").cpu, 4)

    def test_bulk_import_partial_failure(self):
        """测试批量导入部分失败（无效IP）"""
        url = reverse('hosts-bulk-import')
        data = [
            {
                "hostname": "good-host",
                "private_ip": "10.0.0.1",
                "env": self.env.id
            },
            {
                "hostname": "bad-host",
                "private_ip": "invalid-ip", # 触发 Serializer 校验
                "env": self.env.id
            }
        ]
        response = self.client.post(url, data, format='json')
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(Host.objects.count(), 1)
        self.assertTrue(len(response.data['errors']) > 0)

    def test_provider_factory(self):
        """测试服务商工厂类"""
        provider = ProviderFactory.get_provider('aliyun', 'ak', 'sk', 'endpoint')
        self.assertIsInstance(provider, AliyunProvider)
        self.assertEqual(provider.access_key, 'ak')

        unknown = ProviderFactory.get_provider('unknown', 'ak', 'sk')
        self.assertIsNone(unknown)
