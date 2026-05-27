from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase
from django.contrib.auth import get_user_model
from django.utils import timezone
import datetime
from unittest.mock import patch

from apps.pipeline_management.models import Pipeline, PipelineRun
from apps.task_management.models import AnsibleTask, AnsibleExecution
from apps.host_management.models import Environment, Platform, ResourcePool, Host, ComplianceFramework, ComplianceClause, ComplianceBaselineMapping, HostBaseline

class SystemReportsTestCase(APITestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_superuser(username='reportuser', password='password', email='report@test.com')
        self.client.force_authenticate(user=self.user)

        # Setup standard components
        self.env_prod = Environment.objects.create(name="生产环境", code="prod")
        self.platform_ali = Platform.objects.create(name="阿里云", type="aliyun")
        self.pool = ResourcePool.objects.create(name="Web集群", code="web_servers")
        
        self.host = Host.objects.create(
            hostname="web-01",
            private_ip="192.168.1.10",
            env=self.env_prod,
            platform=self.platform_ali
        )
        self.pool.hosts.add(self.host)

        # Setup Pipeline runs
        self.pipeline = Pipeline.objects.create(name="Test Pipeline", timeout=3600)
        self.p_run = PipelineRun.objects.create(
            pipeline=self.pipeline,
            status="success",
            trigger_type="manual",
            start_time=timezone.now() - datetime.timedelta(hours=1),
            end_time=timezone.now()
        )
        # Update create_time to past date to test timezone/time range query
        PipelineRun.objects.filter(id=self.p_run.id).update(create_time=timezone.now() - datetime.timedelta(days=2))

        # Setup Ansible executions
        self.ans_task = AnsibleTask.objects.create(
            name="Deploy Playbook",
            task_type="playbook",
            resource_pool=self.pool
        )
        self.ans_exec = AnsibleExecution.objects.create(
            task=self.ans_task,
            status="success",
            start_time=timezone.now() - datetime.timedelta(minutes=30),
            end_time=timezone.now()
        )
        AnsibleExecution.objects.filter(id=self.ans_exec.id).update(create_time=timezone.now() - datetime.timedelta(days=1))

        # Setup Compliance (等保)
        self.framework = ComplianceFramework.objects.create(name="等保2.0", code="mlps_2.0", version="2.0")
        self.clause = ComplianceClause.objects.create(
            framework=self.framework,
            code="S3.1",
            name="身份鉴别"
        )
        self.baseline = HostBaseline.objects.create(
            name="主机密码强度基线",
            resource_pool=self.pool,
            check_playbook="ping",
            last_check_status="success"
        )
        self.mapping = ComplianceBaselineMapping.objects.create(
            clause=self.clause,
            baseline=self.baseline
        )

    def test_pipeline_stats_api(self):
        url = reverse('system-reports-pipeline')
        # Query last 5 days
        response = self.client.get(url, {
            'start_time': (timezone.now() - datetime.timedelta(days=5)).isoformat(),
            'end_time': timezone.now().isoformat()
        })
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        res_data = response.json()
        self.assertEqual(res_data['code'], 200)
        
        data = res_data['data']
        self.assertEqual(data['summary']['total_runs'], 1)
        self.assertEqual(data['summary']['success_runs'], 1)
        self.assertEqual(data['summary']['success_rate'], 100.0)
        self.assertTrue(len(data['trend']) > 0)
        self.assertTrue(len(data['trigger_distribution']) > 0)

    def test_ansible_stats_api(self):
        url = reverse('system-reports-ansible')
        response = self.client.get(url, {
            'start_time': (timezone.now() - datetime.timedelta(days=5)).isoformat(),
            'end_time': timezone.now().isoformat()
        })
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        res_data = response.json()
        self.assertEqual(res_data['code'], 200)
        
        data = res_data['data']
        self.assertEqual(data['summary']['total_executions'], 1)
        self.assertEqual(data['summary']['success_executions'], 1)
        self.assertEqual(data['summary']['success_rate'], 100.0)
        self.assertEqual(data['summary']['total_host_runs'], 1)  # 1 targeted host
        
        # Check breakdown
        breakdown = data['breakdown']
        self.assertEqual(len(breakdown['environment']), 1)
        self.assertEqual(breakdown['environment'][0]['name'], "生产环境")
        self.assertEqual(breakdown['environment'][0]['count'], 1)
        self.assertEqual(breakdown['environment'][0]['success_rate'], 100.0)

    def test_compliance_stats_api(self):
        url = reverse('system-reports-compliance')
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        res_data = response.json()
        self.assertEqual(res_data['code'], 200)
        
        data = res_data['data']
        self.assertEqual(data['summary']['overall_score'], 100.0)
        self.assertEqual(data['summary']['total_frameworks'], 1)
        self.assertEqual(data['summary']['total_compliance_items'], 1)
        self.assertEqual(data['summary']['failed_compliance_items'], 0)
        self.assertEqual(data['clause_distribution']['success'], 1)

    @patch('apps.system_management.tasks.export_system_report_task.delay')
    def test_export_report_trigger(self, mock_task_delay):
        url = reverse('system-reports-export')
        payload = {
            'export_types': ['pipeline', 'ansible', 'compliance'],
            'start_time': (timezone.now() - datetime.timedelta(days=5)).isoformat(),
            'end_time': timezone.now().isoformat(),
            'filters': {'env_id': self.env_prod.id}
        }
        response = self.client.post(url, payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        mock_task_delay.assert_called_once()

    def test_export_system_report_task_execution(self):
        from apps.system_management.tasks import export_system_report_task
        from apps.system_management.models import UserNotification
        import zipfile
        import os
        from django.conf import settings

        start_time_str = (timezone.now() - datetime.timedelta(days=5)).isoformat()
        end_time_str = timezone.now().isoformat()

        # Run the celery task synchronously
        result_msg = export_system_report_task(
            user_id=self.user.id,
            export_types=['pipeline', 'ansible', 'compliance'],
            start_time_str=start_time_str,
            end_time_str=end_time_str,
            filters={'project_id': self.pipeline.project_id}
        )

        self.assertIn("System report exported successfully", result_msg)

        # Check notification was created
        notification = UserNotification.objects.filter(user=self.user).first()
        self.assertIsNotNone(notification)
        self.assertEqual(notification.extra_data['type'], 'report_ready')
        
        # Verify the file is generated in media/reports/
        download_url = notification.extra_data['download_url']
        filename = download_url.split('/')[-1]
        filepath = os.path.join(settings.MEDIA_ROOT, 'reports', filename)
        self.assertTrue(os.path.exists(filepath))

        # Since we exported multiple items, it should be a ZIP archive
        self.assertTrue(zipfile.is_zipfile(filepath))
        
        with zipfile.ZipFile(filepath, 'r') as zf:
            file_list = zf.namelist()
            self.assertIn('pipeline_execution_report.csv', file_list)
            self.assertIn('ansible_execution_detail.csv', file_list)
            self.assertIn('ansible_execution_summary.csv', file_list)
            self.assertIn('compliance_status_report.csv', file_list)

        # Cleanup generated file
        if os.path.exists(filepath):
            os.remove(filepath)
