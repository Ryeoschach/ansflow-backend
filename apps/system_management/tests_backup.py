import os
import gzip
import json
from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase
from django.contrib.auth import get_user_model
from apps.host_management.models import Host, Environment
from apps.pipeline_management.models import Pipeline
from apps.system_management.backup import BackupExporter, BackupImporter, MODULE_DEFINITIONS

class BackupModularTest(APITestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_superuser(username='backupadmin', password='password', email='backup@test.com')
        self.client.force_authenticate(user=self.user)
        
        # 创建一些跨模块数据
        self.env = Environment.objects.create(name="Backup Test Env", code="bt_env")
        self.host = Host.objects.create(hostname="backup-node", private_ip="10.9.9.9", env=self.env)
        self.pipeline = Pipeline.objects.create(name="Backup Test Pipeline", creator=self.user)

    def test_get_modules(self):
        """测试获取模块列表接口"""
        url = reverse('system-backup-modules')
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        keys = [m['key'] for m in response.data]
        self.assertIn('rbac', keys)
        self.assertIn('host', keys)
        self.assertIn('pipeline', keys)

    def test_modular_export(self):
        """测试按模块导出"""
        exporter = BackupExporter()
        
        # 仅导出 host 模块
        data = exporter.export(selected_modules=['host'])
        
        # 验证包含 Host 和 Environment
        self.assertIn('Host', data['data'])
        self.assertIn('Environment', data['data'])
        
        # 验证不包含 Pipeline (属于 pipeline 模块)
        self.assertNotIn('Pipeline', data['data'])

    def test_modular_restore(self):
        """测试按模块恢复"""
        # 1. 导出全部数据
        exporter = BackupExporter()
        full_data = exporter.export()
        
        # 2. 清理当前数据库数据 (模拟新环境)
        Pipeline.objects.all().delete()
        Host.objects.all().delete()
        Environment.objects.all().delete()
        
        # 3. 仅恢复 pipeline 模块
        importer = BackupImporter(full_data)
        result = importer.import_all(selected_modules=['pipeline'])
        
        self.assertTrue(result['success'])
        # 验证 Pipeline 已恢复
        self.assertTrue(Pipeline.objects.filter(name="Backup Test Pipeline").exists())
        # 验证 Host 未恢复
        self.assertFalse(Host.objects.filter(hostname="backup-node").exists())
