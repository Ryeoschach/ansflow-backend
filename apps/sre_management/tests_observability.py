from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.rbac_permission.models import Project
from apps.sre_management.models import AlertEvent, DiagnosisRun, ObservabilityDataSource, ObservedService
from apps.sre_management.rule_templates import render_template
from apps.sre_management.tasks import run_timepoint_diagnosis


class SREObservabilityTestCase(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_superuser(username='diag_admin', password='password')
        self.project = Project.objects.create(name='诊断项目', code='diag', owner=self.user)
        self.metric_ds = ObservabilityDataSource.objects.create(
            name='VictoriaMetrics',
            type='victoriametrics',
            base_url='http://victoriametrics:8428',
            is_default=True,
        )
        self.log_ds = ObservabilityDataSource.objects.create(
            name='VictoriaLogs',
            type='victorialogs',
            base_url='http://victorialogs:9428',
            is_default=True,
        )
        self.service = ObservedService.objects.create(
            name='订单服务',
            code='order-api',
            project=self.project,
            metric_datasource=self.metric_ds,
            log_datasource=self.log_ds,
            metric_label_selector={'job': 'order-api'},
            log_label_selector={'service': 'order-api'},
        )

    @patch('utils.config_manager.ConfigCache.get')
    @patch('apps.sre_management.views.cache.add')
    @patch('apps.system_management.notifiers.notify_alert_firing')
    def test_vmalert_source_is_detected_from_alertmanager_payload(self, mock_notify, mock_cache_add, mock_config_get):
        mock_config_get.return_value = ''
        mock_cache_add.return_value = False
        url = reverse('sre-alerts-receive')
        payload = {
            'receiver': 'ansflow',
            'externalURL': 'http://vmalert:8880',
            'alerts': [{
                'status': 'firing',
                'fingerprint': 'vm-alert-1',
                'generatorURL': 'http://vmalert:8880/api/v1/alert',
                'labels': {'alertname': 'ServiceDown', 'severity': 'critical'},
                'annotations': {'summary': 'service down'},
            }],
        }

        response = self.client.post(url, payload, content_type='application/json')

        self.assertEqual(response.status_code, 200)
        alert = AlertEvent.objects.get(fingerprint='vm-alert-1')
        self.assertEqual(alert.source, 'vmalert')

    def test_alert_rule_template_render(self):
        rendered = render_template('jvm_heap_high', {'job': 'demo-app', 'threshold': '80'})

        self.assertIn('JVMHeapUsageHigh', rendered['yaml'])
        self.assertIn('job="demo-app"', rendered['yaml'])
        self.assertIn('> 80', rendered['yaml'])

    @patch('apps.sre_management.observability.VictoriaClient.test_connection')
    def test_datasource_connection_api(self, mock_test_connection):
        mock_test_connection.return_value = {'ok': True, 'status_code': 200}
        client = APIClient()
        client.force_authenticate(self.user)
        url = reverse('sre-observability-datasources-test-connection', args=[self.metric_ds.id])

        response = client.post(url)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data['ok'])

    @patch('apps.sre_management.observability.VictoriaClient.query_logs')
    @patch('apps.sre_management.observability.VictoriaClient.query_metrics')
    @patch('apps.ai_engine.rag_service.RAGService.get_chat_chain')
    def test_timepoint_diagnosis_task_collects_context(self, mock_chain_factory, mock_metrics, mock_logs):
        mock_metrics.return_value = [{'name': 'up', 'query': 'up', 'result': []}]
        mock_logs.return_value = {'query': '{service="order-api"}', 'result': {'data': []}}
        chain = MagicMock()
        chain.invoke.return_value = '诊断结论：服务指标和日志已分析。'
        mock_chain_factory.return_value = chain
        run = DiagnosisRun.objects.create(
            title='订单服务时间点诊断',
            project=self.project,
            service=self.service,
            diagnosis_time=timezone.now(),
            window_minutes=10,
            created_by=self.user,
        )

        run_timepoint_diagnosis(run.id)

        run.refresh_from_db()
        self.assertEqual(run.status, 'success')
        self.assertIn('诊断结论', run.ai_result)
        self.assertEqual(run.context_snapshot['service']['code'], 'order-api')
        self.assertEqual(run.context_snapshot['metrics'][0]['name'], 'up')
