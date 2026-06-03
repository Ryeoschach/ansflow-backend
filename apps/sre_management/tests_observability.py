from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.ai_engine.models import AIPromptTemplate
from apps.rbac_permission.models import Project
from apps.sre_management.diagnosis_utils import extract_log_highlights, match_services_for_alert
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

    @patch('apps.sre_management.views.get_observability_adapter')
    def test_datasource_connection_api(self, mock_get_adapter):
        adapter = MagicMock()
        adapter.test_connection.return_value = {'ok': True, 'status_code': 200}
        mock_get_adapter.return_value = adapter
        client = APIClient()
        client.force_authenticate(self.user)
        url = reverse('sre-observability-datasources-test-connection', args=[self.metric_ds.id])

        response = client.post(url)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data['ok'])

    @patch('apps.sre_management.observability.get_log_adapter')
    @patch('apps.sre_management.observability.get_metric_adapter')
    @patch('apps.ai_engine.rag_service.RAGService.get_chat_chain')
    def test_timepoint_diagnosis_task_collects_context(self, mock_chain_factory, mock_get_metric_adapter, mock_get_log_adapter):
        metric_adapter = MagicMock()
        log_adapter = MagicMock()
        metric_adapter.query_metrics.return_value = [{'name': 'up', 'query': 'up', 'result': []}]
        log_adapter.query_logs.return_value = {
            'query': '{service="order-api"}',
            'items': [{'timestamp': 't1', 'level': 'error', 'message': 'Exception timeout failed', 'service': 'order-api'}],
            'result': {'data': []},
        }
        mock_get_metric_adapter.return_value = metric_adapter
        mock_get_log_adapter.return_value = log_adapter
        chain = MagicMock()
        chain.invoke.return_value = (
            '__STRUCTURED_REPORT__:{"summary":"订单服务异常","impact_scope":["订单接口"],'
            '"evidence":[{"ref":"LOG-1","finding":"日志出现异常"}],'
            '"possible_causes":[{"title":"服务超时","confidence":"high","evidence_refs":["LOG-1"]}],'
            '"recommended_actions":[{"action":"检查依赖服务","priority":"high","evidence_refs":["LOG-1"]}],'
            '"risks":["证据窗口有限"],"next_checks":["检查 JVM 指标"]}\n\n'
            '## 诊断结论\n服务指标和日志已分析。'
        )
        mock_chain_factory.return_value = chain
        AIPromptTemplate.objects.update_or_create(
            code='timepoint_diagnosis',
            defaults={
                'name': '自定义时间点诊断模板',
                'description': '测试自定义时间点诊断提示词是否生效',
                'template': '{prefix}\nCUSTOM_TIMEPOINT_PROMPT::{diagnosis_context}',
                'is_system': True,
            },
        )
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
        self.assertEqual(run.context_snapshot['collection_summary']['metrics']['status'], 'success')
        self.assertEqual(run.context_snapshot['collection_summary']['logs']['status'], 'success')
        self.assertEqual(run.context_snapshot['structured_report']['summary'], '订单服务异常')
        self.assertEqual(run.context_snapshot['evidence_index'][0]['ref'], 'LOG-1')
        self.assertNotIn('__STRUCTURED_REPORT__', run.ai_result)
        self.assertIn('CUSTOM_TIMEPOINT_PROMPT::', chain.invoke.call_args[0][0])

    def test_alert_service_label_matches_observed_service_code(self):
        alert = AlertEvent.objects.create(
            alert_name='OrderServiceDown',
            severity='critical',
            fingerprint='match-order-api',
            labels={'service': 'order-api', 'severity': 'critical'},
            annotations={},
        )

        result = match_services_for_alert(alert, project_id=self.project.id)

        self.assertEqual(result['best_match']['id'], self.service.id)
        self.assertGreaterEqual(result['best_match']['score'], result['threshold'])

    def test_multi_label_selector_scores_above_single_label_selector(self):
        single = ObservedService.objects.create(
            name='单标签服务',
            code='single-api',
            project=self.project,
            log_label_selector={'env': 'prod'},
        )
        multi = ObservedService.objects.create(
            name='多标签服务',
            code='multi-api',
            project=self.project,
            log_label_selector={'env': 'prod', 'component': 'checkout'},
        )
        alert = AlertEvent.objects.create(
            alert_name='CheckoutWarning',
            severity='warning',
            fingerprint='match-multi-label',
            labels={'env': 'prod', 'component': 'checkout'},
            annotations={},
        )

        result = match_services_for_alert(alert, project_id=self.project.id)
        scores = {item['id']: item['score'] for item in result['candidates']}

        self.assertGreater(scores[multi.id], scores[single.id])

    def test_low_score_match_returns_candidate_without_best_match(self):
        alert = AlertEvent.objects.create(
            alert_name='EnvOnlyWarning',
            severity='warning',
            fingerprint='match-low-score',
            labels={'env': 'prod'},
            annotations={},
        )
        self.service.log_label_selector = {'env': 'prod'}
        self.service.save(update_fields=['log_label_selector'])

        result = match_services_for_alert(alert, project_id=self.project.id)

        self.assertIsNone(result['best_match'])
        self.assertGreater(len(result['candidates']), 0)
        self.assertLess(result['candidates'][0]['score'], result['threshold'])

    @patch('apps.sre_management.tasks.run_timepoint_diagnosis.delay')
    def test_diagnosis_create_autofills_service_from_alert(self, mock_delay):
        alert = AlertEvent.objects.create(
            alert_name='OrderServiceDown',
            severity='critical',
            fingerprint='diagnosis-auto-service',
            labels={'service': 'order-api'},
            annotations={},
        )
        client = APIClient()
        client.force_authenticate(self.user)
        url = reverse('sre-diagnosis-runs-list')

        response = client.post(url, {
            'title': '告警自动匹配诊断',
            'project': self.project.id,
            'alert': alert.id,
            'trigger_type': 'alert',
            'diagnosis_time': timezone.now().isoformat(),
            'window_minutes': 10,
        }, format='json')

        self.assertEqual(response.status_code, 201)
        run = DiagnosisRun.objects.get(id=response.data['id'])
        self.assertEqual(run.service_id, self.service.id)
        self.assertEqual(run.query_params['service_match']['best_match']['id'], self.service.id)

    @patch('apps.sre_management.observability.get_metric_adapter')
    @patch('apps.ai_engine.rag_service.RAGService.get_chat_chain')
    def test_timepoint_diagnosis_degrades_when_log_datasource_missing(self, mock_chain_factory, mock_get_metric_adapter):
        metric_adapter = MagicMock()
        metric_adapter.query_metrics.return_value = []
        mock_get_metric_adapter.return_value = metric_adapter
        chain = MagicMock()
        chain.invoke.return_value = '诊断结论：日志源缺失，已基于内部上下文分析。'
        mock_chain_factory.return_value = chain
        self.service.log_datasource = None
        self.service.save(update_fields=['log_datasource'])
        self.log_ds.is_default = False
        self.log_ds.save(update_fields=['is_default'])
        run = DiagnosisRun.objects.create(
            title='缺少日志源诊断',
            project=self.project,
            service=self.service,
            diagnosis_time=timezone.now(),
            window_minutes=10,
            created_by=self.user,
        )

        run_timepoint_diagnosis(run.id)

        run.refresh_from_db()
        self.assertEqual(run.status, 'success')
        self.assertEqual(run.context_snapshot['collection_summary']['logs']['status'], 'skipped')
        self.assertIn('未配置日志数据源', run.context_snapshot['warnings'][0])

    def test_log_highlights_extracts_error_items_with_limit(self):
        logs = {
            'items': [
                {'timestamp': f't-{index}', 'level': 'error', 'message': f'Exception timeout failed {index}', 'service': 'order-api'}
                for index in range(40)
            ]
        }

        highlights = extract_log_highlights(logs)

        self.assertEqual(len(highlights), 30)
        self.assertIn('exception', highlights[0]['matched_keywords'])
        self.assertGreater(highlights[0]['score'], 0)

    @patch('apps.sre_management.observability.get_metric_adapter')
    @patch('apps.ai_engine.rag_service.RAGService.get_chat_chain')
    def test_invalid_structured_report_degrades_to_markdown(self, mock_chain_factory, mock_get_metric_adapter):
        metric_adapter = MagicMock()
        metric_adapter.query_metrics.return_value = []
        mock_get_metric_adapter.return_value = metric_adapter
        self.service.log_datasource = None
        self.service.save(update_fields=['log_datasource'])
        self.log_ds.is_default = False
        self.log_ds.save(update_fields=['is_default'])
        chain = MagicMock()
        chain.invoke.return_value = '__STRUCTURED_REPORT__:{invalid json}\n\n## 诊断结论\n仅 Markdown 可用。'
        mock_chain_factory.return_value = chain
        run = DiagnosisRun.objects.create(
            title='非法结构化报告诊断',
            project=self.project,
            service=self.service,
            diagnosis_time=timezone.now(),
            window_minutes=10,
            created_by=self.user,
        )

        run_timepoint_diagnosis(run.id)

        run.refresh_from_db()
        self.assertEqual(run.status, 'success')
        self.assertEqual(run.context_snapshot['structured_report']['summary'], '')
        self.assertIn('结构化诊断报告解析失败', run.context_snapshot['warnings'][-1])
        self.assertNotIn('__STRUCTURED_REPORT__', run.ai_result)
