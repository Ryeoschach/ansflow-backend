from django.test import TestCase
from unittest.mock import patch, MagicMock
from django.utils import timezone
from apps.sre_management.models import AlertEvent, SelfHealingPolicy
from apps.sre_management.tasks import analyze_alert_event, trigger_self_healing
from apps.pipeline_management.models import Pipeline, PipelineRun
from django.contrib.auth import get_user_model

class SREOptimizationTestCase(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_superuser(username='sreadmin', password='password', email='sre@test.com')
        
        self.pipeline = Pipeline.objects.create(
            name="Healing Pipeline",
            creator=self.user,
            graph_data={"nodes": [], "edges": []}
        )
        
        self.alert = AlertEvent.objects.create(
            alert_name="High CPU Usage",
            severity="critical",
            status="firing",
            fingerprint="abc-123",
            labels={"host": "server-01", "service": "web"}
        )

    @patch('apps.ai_engine.rag_service.RAGService.get_chat_chain')
    def test_analyze_alert_noise_reduction(self, mock_get_chain):
        """测试告警分析中的降噪建议逻辑"""
        # 模拟 5 个最近的相同告警
        for i in range(5):
            AlertEvent.objects.create(
                alert_name="High CPU Usage",
                severity="critical",
                status="firing",
                fingerprint="abc-123",
                labels={"host": "server-01"},
                create_time=timezone.now() - timezone.timedelta(minutes=10)
            )
        
        mock_chain = MagicMock()
        mock_chain.invoke.return_value = "Initial Analysis."
        mock_get_chain.return_value = mock_chain
        
        analyze_alert_event(self.alert.id)
        
        self.alert.refresh_from_db()
        self.assertIn("告警降噪建议", self.alert.ai_analysis)
        self.assertIn("发生 5 次", self.alert.ai_analysis)

    @patch('apps.approval_center.engine.ProxyApprovalEngine.intercept_if_needed')
    @patch('apps.pipeline_management.tasks.advance_pipeline_engine.delay')
    def test_trigger_self_healing_variable_injection(self, mock_advance, mock_intercept):
        """测试自愈触发时的变量注入"""
        self.alert.suggested_pipeline = self.pipeline
        self.alert.save()
        
        mock_intercept.return_value = (False, None)
        
        trigger_self_healing(self.alert.id)
        
        # 检查是否创建了带变量的 PipelineRun
        run = PipelineRun.objects.get(pipeline=self.pipeline, trigger_type='automation')
        self.assertIsNotNone(run.extra_vars)
        self.assertEqual(run.extra_vars['alert']['labels']['host'], 'server-01')
        self.assertEqual(run.extra_vars['alert']['name'], 'High CPU Usage')

    def test_variable_resolution_with_alert_context(self):
        """测试流水线变量解析引擎对告警上下文的支持"""
        from apps.pipeline_management.utils import resolve_pipeline_vars
        
        run = PipelineRun.objects.create(
            pipeline=self.pipeline, 
            trigger_user=self.user, 
            status='running',
            extra_vars={
                "alert": {"labels": {"host": "web-prod-01"}}
            }
        )
        
        raw_config = {
            "target": "deploy to {{ alert.labels.host }}"
        }
        
        resolved = resolve_pipeline_vars(raw_config, run.id)
        self.assertEqual(resolved["target"], "deploy to web-prod-01")
