import json
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.ai_engine.models import AIPromptTemplate
from apps.pipeline_management.models import Pipeline, PipelineNodeRun, PipelineRun
from apps.rbac_permission.models import Project
from apps.sre_management.diagnosis_collectors import (
    AnsFlowEventCollector,
    CiCdContextCollector,
    DiagnosisEvidenceBuilder,
)
from apps.sre_management.diagnosis_prompt import DiagnosisPromptContextBuilder
from apps.sre_management.diagnosis_utils import build_evidence_index, extract_log_highlights, match_services_for_alert
from apps.sre_management.models import AlertEvent, DiagnosisRun, DiagnosisTemplate, ObservabilityDataSource, ObservedService
from apps.sre_management.observability import get_log_adapter
from apps.sre_management.rule_templates import render_template
from apps.sre_management.tasks import run_timepoint_diagnosis
from apps.task_management.models import AnsibleExecution, AnsibleTask


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

    def _create_failed_pipeline_node(self, project=None, node_type='build'):
        pipeline = Pipeline.objects.create(
            name=f'诊断流水线-{timezone.now().timestamp()}',
            project=project or self.project,
            creator=self.user,
            graph_data={'nodes': [], 'edges': []},
        )
        run = PipelineRun.objects.create(
            pipeline=pipeline,
            status='failed',
            trigger_user=self.user,
        )
        node = PipelineNodeRun.objects.create(
            run=run,
            node_id='node-build',
            node_type=node_type,
            node_label='构建节点',
            status='failed',
            logs='ERROR build failed',
        )
        return pipeline, run, node

    def _create_ansible_execution(self):
        task = AnsibleTask.objects.create(
            name='诊断 Ansible 任务',
            project=self.project,
            task_type='cmd',
            content='uptime',
            creator=self.user,
        )
        return AnsibleExecution.objects.create(
            task=task,
            status='failed',
            executor=self.user,
            from_pipeline=True,
        )

    def test_ansflow_event_collector_preserves_event_groups_and_summary(self):
        _, pipeline_run, _ = self._create_failed_pipeline_node()
        diagnosis = DiagnosisRun.objects.create(
            title='事件采集器诊断',
            project=self.project,
            diagnosis_time=timezone.now(),
            created_by=self.user,
        )
        context = {
            'ansflow_events': {},
            'collection_summary': {'ansflow_events': {'status': 'pending', 'count': 0}},
        }

        AnsFlowEventCollector().collect_into(
            context,
            diagnosis,
            timezone.now() - timezone.timedelta(minutes=1),
            timezone.now() + timezone.timedelta(minutes=1),
        )

        self.assertEqual(
            set(context['ansflow_events']),
            {'alerts', 'pipeline_runs', 'ansible_executions', 'approval_tickets'},
        )
        self.assertEqual(context['ansflow_events']['pipeline_runs'][0]['id'], pipeline_run.id)
        self.assertEqual(context['collection_summary']['ansflow_events']['status'], 'success')
        self.assertEqual(
            context['collection_summary']['ansflow_events']['count'],
            sum(len(items) for items in context['ansflow_events'].values()),
        )

    def test_ci_cd_context_collector_preserves_pipeline_and_log_shape(self):
        template = DiagnosisTemplate.objects.get(code='ci_pipeline_failure', scope='global')
        _, pipeline_run, node_run = self._create_failed_pipeline_node()
        diagnosis = DiagnosisRun.objects.create(
            title='CI/CD 采集器诊断',
            project=self.project,
            template=template,
            diagnosis_time=timezone.now(),
            query_params={
                'pipeline_run_id': pipeline_run.id,
                'pipeline_node_run_id': node_run.id,
                'template_snapshot': template.to_snapshot(),
            },
            created_by=self.user,
        )
        context = {
            'ci_cd_context': {},
            'collection_summary': {'ci_cd_context': {'status': 'skipped', 'count': 0}},
        }

        CiCdContextCollector().collect_into(
            context,
            diagnosis,
            timezone.now() - timezone.timedelta(minutes=1),
            timezone.now() + timezone.timedelta(minutes=1),
            template.to_snapshot(),
        )

        ci_cd_context = context['ci_cd_context']
        self.assertEqual(ci_cd_context['pipeline_run']['id'], pipeline_run.id)
        self.assertEqual(ci_cd_context['failed_nodes'][0]['id'], node_run.id)
        self.assertEqual(ci_cd_context['node_log_highlights'][0]['line'], 'ERROR build failed')
        self.assertEqual(ci_cd_context['node_log_highlights'][0]['node_run_id'], node_run.id)
        self.assertEqual(context['collection_summary']['ci_cd_context']['status'], 'success')

    def test_diagnosis_evidence_builder_keeps_legacy_utility_compatible(self):
        context = {
            'log_highlights': [{'message': 'timeout', 'service': 'order-api'}],
            'metric_contexts': [{
                'datasource': {'id': self.metric_ds.id, 'name': self.metric_ds.name},
                'metrics': [{'name': 'up', 'query': 'up'}],
            }],
            'ansflow_events': {},
            'ci_cd_context': {},
        }

        evidence = DiagnosisEvidenceBuilder().build(context)

        self.assertEqual(evidence, build_evidence_index(context))
        self.assertEqual([item['ref'] for item in evidence], ['LOG-1', f'metric:{self.metric_ds.id}:1'])

    def test_prompt_context_builder_keeps_valid_json_and_prioritizes_evidence(self):
        high_priority_message = 'critical timeout ' + ('x' * 900)
        context = {
            'diagnosis': {'id': 1, 'title': '超大上下文诊断'},
            'project': {'id': self.project.id, 'name': self.project.name},
            'template': {'code': 'ci_pipeline_failure'},
            'service': {'id': self.service.id, 'code': self.service.code},
            'warnings': [],
            'collection_summary': {},
            'log_highlights': [],
            'log_contexts': [{
                'datasource': {'id': self.log_ds.id, 'name': self.log_ds.name},
                'query': '{service="order-api"}',
                'items': [{'message': 'raw log ' + ('y' * 2000)} for _ in range(20)],
                'highlights': [
                    {
                        'evidence_id': f'log:{self.log_ds.id}:{index}',
                        'message': high_priority_message if index == 0 else f'warning {index} ' + ('z' * 900),
                        'score': 100 if index == 0 else index,
                    }
                    for index in range(20)
                ],
            }],
            'metric_contexts': [{
                'datasource': {'id': self.metric_ds.id, 'name': self.metric_ds.name},
                'metrics': [
                    {
                        'name': f'metric_{index}',
                        'query': f'metric_{index}{{service="order-api"}}',
                        'result': [{'value': 'm' * 1000} for _ in range(10)],
                    }
                    for index in range(10)
                ],
            }],
            'ansflow_events': {'alerts': [], 'pipeline_runs': [], 'ansible_executions': [], 'approval_tickets': []},
            'ci_cd_context': {},
            'evidence_index': [
                {
                    'ref': f'LOG-{index + 1}',
                    'type': 'log',
                    'title': f'Log {index + 1}',
                    'summary': high_priority_message if index == 0 else f'warning {index}',
                    'raw': {'score': 100 if index == 0 else index, 'payload': 'r' * 2000},
                }
                for index in range(20)
            ],
        }

        prompt_context, summary = DiagnosisPromptContextBuilder(max_chars=5000).build(context)
        parsed = json.loads(prompt_context)

        self.assertLessEqual(len(prompt_context), 5000)
        self.assertIn('critical timeout', parsed['logs']['highlights'][0]['message'])
        self.assertNotIn('"raw"', prompt_context)
        self.assertTrue(summary['compressed'])
        self.assertTrue(summary['truncated'])
        self.assertGreater(summary['removed_count'], 0)
        self.assertEqual(summary['budget_chars'], 5000)

        minimal_prompt, minimal_summary = DiagnosisPromptContextBuilder(max_chars=1000).build({
            **context,
            'source_alert': {'labels': {f'label_{index}': 'v' * 5000 for index in range(50)}},
        })
        json.loads(minimal_prompt)
        self.assertLessEqual(len(minimal_prompt), 1000)
        self.assertEqual(minimal_summary['budget_chars'], 1000)

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

    def test_datasource_capabilities_api_exposes_log_providers(self):
        client = APIClient()
        client.force_authenticate(self.user)
        url = reverse('sre-observability-datasources-capabilities')

        response = client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertIn('generic_http', response.data)
        self.assertTrue(response.data['generic_http']['supports_logs'])
        self.assertIn('response_mapping', response.data['elasticsearch'])

    @patch('apps.sre_management.views.get_log_adapter')
    def test_observed_service_preview_logs_api(self, mock_get_log_adapter):
        adapter = MagicMock()
        adapter.query_logs.return_value = {
            'query': '{service="order-api"}',
            'items': [{'timestamp': 't1', 'level': 'error', 'message': 'boom', 'service': 'order-api'}],
            'result': {'data': []},
        }
        mock_get_log_adapter.return_value = adapter
        client = APIClient()
        client.force_authenticate(self.user)
        url = reverse('sre-observed-services-preview-logs', args=[self.service.id])

        response = client.post(url, {'window_minutes': 5, 'limit': 5}, format='json')

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data['ok'])
        self.assertEqual(response.data['type'], 'logs')
        self.assertEqual(response.data['query'], '{service="order-api"}')
        self.assertEqual(response.data['items'][0]['message'], 'boom')
        self.assertEqual(response.data['datasource']['provider'], 'victorialogs')

    @patch('apps.sre_management.views.get_metric_adapter')
    def test_observed_service_preview_metrics_api(self, mock_get_metric_adapter):
        adapter = MagicMock()
        adapter.query_metrics.return_value = [{'name': 'up', 'query': 'up{job="order-api"}', 'result': []}]
        mock_get_metric_adapter.return_value = adapter
        client = APIClient()
        client.force_authenticate(self.user)
        url = reverse('sre-observed-services-preview-metrics', args=[self.service.id])

        response = client.post(url, {'window_minutes': 5, 'step': '30s'}, format='json')

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data['ok'])
        self.assertEqual(response.data['type'], 'metrics')
        self.assertEqual(response.data['metrics'][0]['name'], 'up')
        self.assertEqual(response.data['datasource']['provider'], 'victoriametrics')

    @patch('apps.sre_management.observability.requests.request')
    def test_generic_http_log_adapter_uses_templates_and_normalizes_items(self, mock_request):
        datasource = ObservabilityDataSource.objects.create(
            name='Generic Logs',
            kind='log',
            provider='generic_http',
            type='generic_http',
            base_url='http://logs-gateway:8080',
            auth_type='query',
            query_config={
                'method': 'GET',
                'path': '/search',
                'query_param': 'q',
                'params': {'service': '{{service.code}}', 'env': '{{label.env}}'},
                'auth_params': {'api_key': 'secret'},
            },
            field_mapping={
                'timestamp': 'ts',
                'level': 'severity',
                'message': 'body',
                'service': 'svc',
                'instance': 'host',
            },
            response_mapping={'items_path': 'data.items'},
        )
        self.service.log_label_selector = {'env': 'prod'}
        self.service.log_query = 'error OR exception'
        self.service.save(update_fields=['log_label_selector', 'log_query'])
        response = MagicMock()
        response.json.return_value = {
            'data': {
                'items': [{
                    'ts': '2026-06-04T10:00:00Z',
                    'severity': 'error',
                    'body': 'boom',
                    'svc': 'order-api',
                    'host': 'pod-1',
                }]
            }
        }
        response.raise_for_status.return_value = None
        mock_request.return_value = response

        result = get_log_adapter(datasource).query_logs(
            self.service,
            timezone.now(),
            timezone.now(),
            limit=5,
        )

        self.assertEqual(result['items'][0]['message'], 'boom')
        self.assertEqual(result['items'][0]['service'], 'order-api')
        request_kwargs = mock_request.call_args.kwargs
        self.assertEqual(request_kwargs['params']['service'], 'order-api')
        self.assertEqual(request_kwargs['params']['env'], 'prod')
        self.assertEqual(request_kwargs['params']['api_key'], 'secret')
        self.assertEqual(request_kwargs['params']['q'], 'error OR exception')

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
        self.assertEqual(run.context_snapshot['metric_contexts'][0]['datasource']['id'], self.metric_ds.id)
        self.assertEqual(run.context_snapshot['collection_summary']['metrics']['status'], 'success')
        self.assertEqual(run.context_snapshot['collection_summary']['logs']['status'], 'success')
        self.assertEqual(run.context_snapshot['collection_summary']['prompt_context']['status'], 'success')
        self.assertLessEqual(
            run.context_snapshot['collection_summary']['prompt_context']['final_chars'],
            run.context_snapshot['collection_summary']['prompt_context']['budget_chars'],
        )
        self.assertEqual(run.context_snapshot['log_contexts'][0]['datasource']['id'], self.log_ds.id)
        self.assertEqual(run.context_snapshot['structured_report']['summary'], '订单服务异常')
        self.assertEqual(run.context_snapshot['evidence_index'][0]['ref'], 'LOG-1')
        self.assertNotIn('__STRUCTURED_REPORT__', run.ai_result)
        self.assertIn('CUSTOM_TIMEPOINT_PROMPT::', chain.invoke.call_args[0][0])

    @patch('apps.sre_management.observability.get_log_adapter')
    @patch('apps.sre_management.observability.get_metric_adapter')
    @patch('apps.ai_engine.rag_service.RAGService.get_chat_chain')
    def test_timepoint_diagnosis_collects_multiple_log_datasources(self, mock_chain_factory, mock_get_metric_adapter, mock_get_log_adapter):
        second_log_ds = ObservabilityDataSource.objects.create(
            name='Loki',
            type='loki',
            provider='loki',
            kind='log',
            base_url='http://loki:3100',
            is_active=True,
        )
        template = DiagnosisTemplate.objects.create(
            scope='global',
            code='multi_log_template',
            name='多日志源诊断',
            category='ci_cd',
            content={
                'target_type': 'service_regression',
                'context_collection': {'metrics': False, 'service_logs': True},
                'log_datasource_ids': [self.log_ds.id, second_log_ds.id],
                'prompt_template': '{prefix}\n{diagnosis_context}',
            },
        )
        first_adapter = MagicMock()
        first_adapter.query_logs.return_value = {
            'query': '{service="order-api"}',
            'items': [{'timestamp': 't1', 'level': 'error', 'message': 'VictoriaLogs exception', 'service': 'order-api'}],
            'result': {'data': []},
        }
        second_adapter = MagicMock()
        second_adapter.query_logs.return_value = {
            'query': '{service="order-api"}',
            'items': [{'timestamp': 't2', 'level': 'fatal', 'message': 'Loki timeout failed', 'service': 'order-api'}],
            'result': {'data': []},
        }
        mock_get_log_adapter.side_effect = [first_adapter, second_adapter]
        chain = MagicMock()
        chain.invoke.return_value = '## 诊断结论\n多日志源已分析。'
        mock_chain_factory.return_value = chain
        run = DiagnosisRun.objects.create(
            title='多日志源诊断',
            project=self.project,
            service=self.service,
            template=template,
            diagnosis_time=timezone.now(),
            window_minutes=10,
            created_by=self.user,
            query_params={'template_snapshot': template.to_snapshot()},
        )

        run_timepoint_diagnosis(run.id)

        run.refresh_from_db()
        self.assertEqual(run.status, 'success')
        self.assertEqual(run.context_snapshot['collection_summary']['logs']['status'], 'success')
        self.assertEqual(run.context_snapshot['collection_summary']['logs']['source_count'], 2)
        self.assertEqual(len(run.context_snapshot['log_contexts']), 2)
        evidence_refs = [item['ref'] for item in run.context_snapshot['evidence_index']]
        self.assertIn(f'log:{self.log_ds.id}:1', evidence_refs)
        self.assertIn(f'log:{second_log_ds.id}:1', evidence_refs)

    @patch('apps.sre_management.observability.get_log_adapter')
    @patch('apps.sre_management.observability.get_metric_adapter')
    @patch('apps.ai_engine.rag_service.RAGService.get_chat_chain')
    def test_timepoint_diagnosis_collects_multiple_metric_datasources(self, mock_chain_factory, mock_get_metric_adapter, mock_get_log_adapter):
        second_metric_ds = ObservabilityDataSource.objects.create(
            name='VictoriaMetrics Backup',
            type='victoriametrics',
            provider='victoriametrics',
            kind='metric',
            base_url='http://victoriametrics-backup:8428',
            is_active=True,
        )
        template = DiagnosisTemplate.objects.create(
            scope='global',
            code='multi_metric_template',
            name='多指标源诊断',
            category='ci_cd',
            content={
                'target_type': 'service_regression',
                'context_collection': {'metrics': True, 'service_logs': False},
                'metric_datasource_ids': [self.metric_ds.id, second_metric_ds.id],
                'prompt_template': '{prefix}\n{diagnosis_context}',
            },
        )
        first_adapter = MagicMock()
        first_adapter.query_metrics.return_value = [{'name': 'up', 'query': 'up', 'result': []}]
        second_adapter = MagicMock()
        second_adapter.query_metrics.return_value = [{'name': 'cpu_usage', 'query': 'rate(cpu[5m])', 'result': []}]
        mock_get_metric_adapter.side_effect = [first_adapter, second_adapter]
        chain = MagicMock()
        chain.invoke.return_value = '## 诊断结论\n多指标源已分析。'
        mock_chain_factory.return_value = chain
        run = DiagnosisRun.objects.create(
            title='多指标源诊断',
            project=self.project,
            service=self.service,
            template=template,
            diagnosis_time=timezone.now(),
            window_minutes=10,
            created_by=self.user,
            query_params={'template_snapshot': template.to_snapshot()},
        )

        run_timepoint_diagnosis(run.id)

        run.refresh_from_db()
        self.assertEqual(run.status, 'success')
        self.assertEqual(run.context_snapshot['collection_summary']['metrics']['status'], 'success')
        self.assertEqual(run.context_snapshot['collection_summary']['metrics']['source_count'], 2)
        self.assertEqual(len(run.context_snapshot['metric_contexts']), 2)
        evidence_refs = [item['ref'] for item in run.context_snapshot['evidence_index']]
        self.assertIn(f'metric:{self.metric_ds.id}:up', evidence_refs)
        self.assertIn(f'metric:{second_metric_ds.id}:cpu_usage', evidence_refs)

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

    def test_builtin_diagnosis_template_cannot_be_deleted(self):
        template = DiagnosisTemplate.objects.get(code='ci_pipeline_failure', scope='global')
        client = APIClient()
        client.force_authenticate(self.user)
        url = reverse('sre-diagnosis-templates-detail', args=[template.id])

        response = client.delete(url)

        self.assertEqual(response.status_code, 400)
        self.assertTrue(DiagnosisTemplate.objects.filter(id=template.id).exists())

    def test_diagnosis_template_requires_supported_target_type(self):
        client = APIClient()
        client.force_authenticate(self.user)
        url = reverse('sre-diagnosis-templates-list')

        response = client.post(url, {
            'scope': 'global',
            'code': 'invalid_target_template',
            'name': '非法目标模板',
            'category': 'ci_cd',
            'content': {
                'target_type': 'unknown',
                'prompt_template': '{prefix}\n{diagnosis_context}',
            },
        }, format='json')

        self.assertEqual(response.status_code, 400)
        self.assertIn('content', response.data['message'])

    def test_diagnosis_template_prompt_must_include_context_placeholder(self):
        client = APIClient()
        client.force_authenticate(self.user)
        url = reverse('sre-diagnosis-templates-list')

        response = client.post(url, {
            'scope': 'global',
            'code': 'invalid_prompt_template',
            'name': '非法 Prompt 模板',
            'category': 'ci_cd',
            'content': {
                'target_type': 'pipeline_run',
                'prompt_template': '{prefix}\n缺少上下文占位符',
            },
        }, format='json')

        self.assertEqual(response.status_code, 400)
        self.assertIn('content', response.data['message'])

    def test_diagnosis_template_accepts_active_log_datasources(self):
        client = APIClient()
        client.force_authenticate(self.user)
        url = reverse('sre-diagnosis-templates-list')

        response = client.post(url, {
            'scope': 'global',
            'code': 'valid_log_datasource_template',
            'name': '有效日志源模板',
            'category': 'ci_cd',
            'content': {
                'target_type': 'service_regression',
                'log_datasource_ids': [self.log_ds.id],
                'prompt_template': '{prefix}\n{diagnosis_context}',
            },
        }, format='json')

        self.assertEqual(response.status_code, 201)
        template = DiagnosisTemplate.objects.get(code='valid_log_datasource_template')
        self.assertEqual(template.content['log_datasource_ids'], [self.log_ds.id])

    def test_diagnosis_template_rejects_non_log_datasources(self):
        client = APIClient()
        client.force_authenticate(self.user)
        url = reverse('sre-diagnosis-templates-list')

        response = client.post(url, {
            'scope': 'global',
            'code': 'invalid_log_datasource_template',
            'name': '非法日志源模板',
            'category': 'ci_cd',
            'content': {
                'target_type': 'service_regression',
                'log_datasource_ids': [self.metric_ds.id],
                'prompt_template': '{prefix}\n{diagnosis_context}',
            },
        }, format='json')

        self.assertEqual(response.status_code, 400)
        self.assertIn('log_datasource_ids', response.data['message'])

    def test_diagnosis_template_accepts_active_metric_datasources(self):
        client = APIClient()
        client.force_authenticate(self.user)
        url = reverse('sre-diagnosis-templates-list')

        response = client.post(url, {
            'scope': 'global',
            'code': 'valid_metric_datasource_template',
            'name': '有效指标源模板',
            'category': 'ci_cd',
            'content': {
                'target_type': 'service_regression',
                'metric_datasource_ids': [self.metric_ds.id],
                'prompt_template': '{prefix}\n{diagnosis_context}',
            },
        }, format='json')

        self.assertEqual(response.status_code, 201)
        template = DiagnosisTemplate.objects.get(code='valid_metric_datasource_template')
        self.assertEqual(template.content['metric_datasource_ids'], [self.metric_ds.id])

    def test_diagnosis_template_rejects_non_metric_datasources(self):
        client = APIClient()
        client.force_authenticate(self.user)
        url = reverse('sre-diagnosis-templates-list')

        response = client.post(url, {
            'scope': 'global',
            'code': 'invalid_metric_datasource_template',
            'name': '非法指标源模板',
            'category': 'ci_cd',
            'content': {
                'target_type': 'service_regression',
                'metric_datasource_ids': [self.log_ds.id],
                'prompt_template': '{prefix}\n{diagnosis_context}',
            },
        }, format='json')

        self.assertEqual(response.status_code, 400)
        self.assertIn('metric_datasource_ids', response.data['message'])

    def test_project_template_overrides_global_template_in_list(self):
        DiagnosisTemplate.objects.create(
            scope='project',
            project=self.project,
            code='ci_pipeline_failure',
            name='项目流水线失败诊断',
            category='ci_cd',
            content={'target_type': 'pipeline_run'},
        )
        client = APIClient()
        client.force_authenticate(self.user)
        url = reverse('sre-diagnosis-templates-list')

        response = client.get(url, {'project': self.project.id, 'page_size': 100})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['total'], 3)
        self.assertTrue(DiagnosisTemplate.objects.filter(
            scope='project',
            project=self.project,
            code='ci_pipeline_failure',
        ).exists())

    @patch('apps.sre_management.tasks.run_timepoint_diagnosis.delay')
    def test_diagnosis_create_with_template_saves_snapshot_and_targets(self, mock_delay):
        template = DiagnosisTemplate.objects.get(code='ci_pipeline_failure', scope='global')
        _, pipeline_run, node_run = self._create_failed_pipeline_node()
        client = APIClient()
        client.force_authenticate(self.user)
        url = reverse('sre-diagnosis-runs-list')

        response = client.post(url, {
            'title': '模板化流水线诊断',
            'project': self.project.id,
            'template': template.id,
            'pipeline_run_id': pipeline_run.id,
            'pipeline_node_run_id': node_run.id,
            'diagnosis_time': timezone.now().isoformat(),
            'window_minutes': 10,
        }, format='json')

        self.assertEqual(response.status_code, 201)
        run = DiagnosisRun.objects.get(id=response.data['id'])
        self.assertEqual(run.template_id, template.id)
        self.assertEqual(run.query_params['template_snapshot']['code'], 'ci_pipeline_failure')
        self.assertIn('collection_plan', run.query_params)
        self.assertEqual(str(run.query_params['collection_plan']['target']['pipeline_run_id']), str(pipeline_run.id))
        self.assertEqual(str(run.query_params['pipeline_run_id']), str(pipeline_run.id))
        self.assertEqual(str(run.query_params['pipeline_node_run_id']), str(node_run.id))

    def test_diagnosis_preview_resolves_template_targets_and_datasources(self):
        DiagnosisTemplate.objects.create(
            scope='global',
            code='preview_template',
            name='采集预览模板',
            category='ci_cd',
            content={
                'target_type': 'pipeline_run',
                'context_collection': {'metrics': True, 'service_logs': True, 'pipeline_run': True},
                'prompt_template': '{prefix}\n{diagnosis_context}',
            },
        )
        _, pipeline_run, node_run = self._create_failed_pipeline_node()
        client = APIClient()
        client.force_authenticate(self.user)
        url = reverse('sre-diagnosis-runs-preview')

        response = client.post(url, {
            'title': '采集预览',
            'project': self.project.id,
            'service': self.service.id,
            'template_code': 'preview_template',
            'pipeline_node_run_id': node_run.id,
            'diagnosis_time': timezone.now().isoformat(),
            'window_minutes': 10,
        }, format='json')

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data['ok'])
        self.assertEqual(response.data['template']['code'], 'preview_template')
        self.assertEqual(str(response.data['target']['pipeline_run_id']), str(pipeline_run.id))
        self.assertEqual(response.data['service']['code'], 'order-api')
        self.assertEqual(response.data['collection']['metrics']['datasources'][0]['id'], self.metric_ds.id)
        self.assertEqual(response.data['collection']['logs']['datasources'][0]['id'], self.log_ds.id)
        self.assertTrue(any(item['key'] == 'pipeline_run' for item in response.data['collection']['ci_cd_context']))

    @patch('apps.sre_management.tasks.run_timepoint_diagnosis.delay')
    def test_diagnosis_create_with_template_code_prefers_project_template(self, mock_delay):
        project_template = DiagnosisTemplate.objects.create(
            scope='project',
            project=self.project,
            code='ci_pipeline_failure',
            name='项目流水线失败诊断',
            category='ci_cd',
            content={'target_type': 'pipeline_run'},
        )
        _, pipeline_run, node_run = self._create_failed_pipeline_node()
        client = APIClient()
        client.force_authenticate(self.user)
        url = reverse('sre-diagnosis-runs-list')

        response = client.post(url, {
            'title': '按编码创建模板诊断',
            'template_code': 'ci_pipeline_failure',
            'pipeline_run_id': pipeline_run.id,
            'pipeline_node_run_id': node_run.id,
            'diagnosis_time': timezone.now().isoformat(),
            'window_minutes': 10,
        }, format='json')

        self.assertEqual(response.status_code, 201)
        run = DiagnosisRun.objects.get(id=response.data['id'])
        self.assertEqual(run.template_id, project_template.id)
        self.assertEqual(run.project_id, self.project.id)

    @patch('apps.sre_management.tasks.run_timepoint_diagnosis.delay')
    def test_diagnosis_create_infers_pipeline_run_from_node_run(self, mock_delay):
        _, pipeline_run, node_run = self._create_failed_pipeline_node()
        client = APIClient()
        client.force_authenticate(self.user)
        url = reverse('sre-diagnosis-runs-list')

        response = client.post(url, {
            'title': '节点推导流水线诊断',
            'template_code': 'ci_pipeline_failure',
            'pipeline_node_run_id': node_run.id,
            'diagnosis_time': timezone.now().isoformat(),
            'window_minutes': 10,
        }, format='json')

        self.assertEqual(response.status_code, 201)
        run = DiagnosisRun.objects.get(id=response.data['id'])
        self.assertEqual(str(run.query_params['pipeline_run_id']), str(pipeline_run.id))
        self.assertEqual(run.project_id, self.project.id)

    @patch('apps.sre_management.tasks.run_timepoint_diagnosis.delay')
    def test_diagnosis_create_infers_ansible_execution_from_node_output(self, mock_delay):
        execution = self._create_ansible_execution()
        _, pipeline_run, node_run = self._create_failed_pipeline_node(node_type='ansible')
        node_run.output_data = {'ansible_execution_id': execution.id, 'status': 'failed'}
        node_run.save(update_fields=['output_data'])
        client = APIClient()
        client.force_authenticate(self.user)
        url = reverse('sre-diagnosis-runs-list')

        response = client.post(url, {
            'title': 'Ansible 节点自动关联诊断',
            'template_code': 'ci_ansible_failure',
            'pipeline_node_run_id': node_run.id,
            'diagnosis_time': timezone.now().isoformat(),
            'window_minutes': 10,
        }, format='json')

        self.assertEqual(response.status_code, 201)
        run = DiagnosisRun.objects.get(id=response.data['id'])
        self.assertEqual(str(run.query_params['pipeline_run_id']), str(pipeline_run.id))
        self.assertEqual(str(run.query_params['ansible_execution_id']), str(execution.id))

    def test_diagnosis_create_rejects_node_run_pipeline_mismatch(self):
        _, first_run, node_run = self._create_failed_pipeline_node()
        _, second_run, _ = self._create_failed_pipeline_node()
        client = APIClient()
        client.force_authenticate(self.user)
        url = reverse('sre-diagnosis-runs-list')

        response = client.post(url, {
            'title': '节点流水线不匹配',
            'template_code': 'ci_pipeline_failure',
            'pipeline_run_id': second_run.id,
            'pipeline_node_run_id': node_run.id,
            'diagnosis_time': timezone.now().isoformat(),
            'window_minutes': 10,
        }, format='json')

        self.assertEqual(response.status_code, 400)
        self.assertEqual(PipelineRun.objects.get(id=first_run.id).id, first_run.id)

    @patch('apps.sre_management.observability.get_metric_adapter')
    @patch('apps.ai_engine.rag_service.RAGService.get_chat_chain')
    def test_template_prompt_format_error_degrades_without_failing_run(self, mock_chain_factory, mock_get_metric_adapter):
        template = DiagnosisTemplate.objects.create(
            scope='project',
            project=self.project,
            code='bad_prompt',
            name='坏 Prompt 模板',
            category='ci_cd',
            content={
                'target_type': 'pipeline_run',
                'context_collection': {'metrics': False, 'service_logs': False},
                'prompt_template': '{prefix}\n{missing_variable}\n{diagnosis_context}',
            },
        )
        metric_adapter = MagicMock()
        metric_adapter.query_metrics.return_value = []
        mock_get_metric_adapter.return_value = metric_adapter
        chain = MagicMock()
        chain.invoke.return_value = '诊断结论：Prompt 已降级。'
        mock_chain_factory.return_value = chain
        run = DiagnosisRun.objects.create(
            title='坏 Prompt 降级诊断',
            project=self.project,
            service=self.service,
            template=template,
            diagnosis_time=timezone.now(),
            window_minutes=10,
            query_params={'template_snapshot': template.to_snapshot()},
            created_by=self.user,
        )

        run_timepoint_diagnosis(run.id)

        run.refresh_from_db()
        self.assertEqual(run.status, 'success')
        self.assertIn('诊断模板 Prompt 格式化失败', ''.join(run.context_snapshot['warnings']))
