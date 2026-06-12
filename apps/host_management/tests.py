from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase
from unittest.mock import MagicMock, patch

from apps.host_management.models import (
    Environment,
    Host,
    HostBaseline,
    Platform,
    ResourcePool,
    SshCredential,
)
from apps.host_management.providers.cloud import AliyunProvider
from apps.host_management.providers.factory import ProviderFactory
from apps.host_management.tasks import (
    check_host_baseline,
    check_host_connectivity,
    sync_platform_assets,
)
from apps.rbac_permission.models import Project
from apps.sre_management.models import AlertEvent
from apps.task_management.models import AnsibleTask


class HostManagementTest(APITestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model

        User = get_user_model()
        self.user = User.objects.create_superuser(
            username='testadmin',
            password='password',
            email='test@test.com',
        )
        self.project = Project.objects.create(
            name='Host Test Project',
            code='host-test',
            owner=self.user,
        )
        self.env = Environment.objects.create(
            name='Test Env',
            code='test_env',
            color='#ff0000',
        )
        self.platform = Platform.objects.create(
            name='Test Aliyun',
            type='aliyun',
            project=self.project,
        )
        self.client.force_authenticate(user=self.user)

    def test_bulk_import_success(self):
        url = reverse('hosts-bulk-import')
        data = [
            {
                'hostname': 'bulk-host-1',
                'private_ip': '10.0.0.1',
                'env': self.env.id,
                'cpu': 4,
            },
            {
                'hostname': 'bulk-host-2',
                'private_ip': '10.0.0.2',
                'env': self.env.id,
                'platform': self.platform.id,
            },
        ]
        response = self.client.post(
            url,
            data,
            format='json',
            HTTP_X_PROJECT_ID=str(self.project.id),
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['status'], 'success')
        self.assertEqual(Host.objects.count(), 2)
        self.assertEqual(Host.objects.get(hostname='bulk-host-1').cpu, 4)
        self.assertFalse(Host.objects.exclude(project=self.project).exists())

    def test_bulk_import_partial_failure(self):
        url = reverse('hosts-bulk-import')
        data = [
            {
                'hostname': 'good-host',
                'private_ip': '10.0.0.1',
                'env': self.env.id,
            },
            {
                'hostname': 'bad-host',
                'private_ip': 'invalid-ip',
                'env': self.env.id,
            },
        ]
        response = self.client.post(
            url,
            data,
            format='json',
            HTTP_X_PROJECT_ID=str(self.project.id),
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(Host.objects.count(), 1)
        self.assertTrue(response.data['errors'])

    def test_provider_factory(self):
        provider = ProviderFactory.get_provider(
            'aliyun',
            'ak',
            'sk',
            'endpoint',
        )
        self.assertIsInstance(provider, AliyunProvider)
        self.assertEqual(provider.access_key, 'ak')
        self.assertIsNone(
            ProviderFactory.get_provider('unknown', 'ak', 'sk')
        )


class HostTaskTestCase(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model

        User = get_user_model()
        self.user = User.objects.create_user(username='host-task-user')
        self.project = Project.objects.create(
            name='Host Task Project',
            code='host-task',
            owner=self.user,
        )
        self.env = Environment.objects.create(
            name='Host Task Env',
            code='host_task_env',
        )
        self.credential = SshCredential.objects.create(
            name='Host Task Credential',
            username='root',
            auth_type='password',
            password='secret',
            project=self.project,
        )
        self.platform = Platform.objects.create(
            name='Host Task Platform',
            type='aliyun',
            project=self.project,
            default_credential=self.credential,
        )

    @patch('apps.host_management.tasks.ProviderFactory.get_provider')
    def test_platform_sync_keeps_hosts_in_platform_project(self, get_provider):
        provider = MagicMock()
        provider.sync_assets.return_value = [{
            'hostname': 'synced-host',
            'private_ip': '10.10.0.8',
            'cpu': 4,
            'memory': 8,
        }]
        get_provider.return_value = provider

        sync_platform_assets(self.platform.id)

        host = Host.objects.get(hostname='synced-host')
        self.assertEqual(host.project, self.project)
        self.assertEqual(host.platform, self.platform)

    @patch('paramiko.SSHClient')
    def test_failed_host_can_recover_after_connectivity_check(self, ssh_client):
        host = Host.objects.create(
            hostname='recover-host',
            private_ip='10.10.0.9',
            env=self.env,
            platform=self.platform,
            credential=self.credential,
            project=self.project,
            status=2,
        )
        ssh_client.return_value.connect.return_value = None

        check_host_connectivity()

        host.refresh_from_db()
        self.assertEqual(host.status, 1)

    @patch('apps.task_management.tasks.run_ansible_task')
    def test_failed_baseline_creates_current_alert_shape(self, run_ansible_task):
        pool = ResourcePool.objects.create(
            name='Baseline Pool',
            code='baseline_pool',
            project=self.project,
        )
        baseline = HostBaseline.objects.create(
            name='SSH Baseline',
            resource_pool=pool,
            check_playbook='- hosts: all\n  tasks: []',
        )
        run_ansible_task.return_value = {
            'status': 'failed',
            'logs': 'baseline failed',
        }

        check_host_baseline(baseline.id)

        alert = AlertEvent.objects.get()
        self.assertEqual(alert.severity, 'critical')
        self.assertEqual(alert.status, 'firing')
        self.assertEqual(alert.labels['project_id'], self.project.id)
        self.assertEqual(alert.labels['baseline_id'], baseline.id)
        self.assertTrue(
            AnsibleTask.objects.filter(project=self.project).exists()
        )
