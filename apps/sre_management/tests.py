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

    @patch('apps.ai_engine.rag_service.RAGService.get_chat_chain')
    def test_analyze_alert_event_dynamic_pipeline_creation(self, mock_get_chain):
        """测试 AI 诊断产生 __PIPELINE_DRAFT__ 时的动态流水线及依赖创建和节点排版排布"""
        mock_chain = MagicMock()
        draft_json = (
            "{\n"
            "  \"nodes\": [\n"
            "    {\n"
            "      \"id\": \"node_1\",\n"
            "      \"type\": \"ansible\",\n"
            "      \"data\": {\n"
            "        \"content\": \"- name: restart nginx\\n  hosts: all\\n  tasks:\\n    - name: restart\\n      service: name=nginx state=restarted\"\n"
            "      }\n"
            "    },\n"
            "    {\n"
            "      \"id\": \"node_2\",\n"
            "      \"type\": \"docker_build\",\n"
            "      \"data\": {}\n"
            "    }\n"
            "  ],\n"
            "  \"edges\": [\n"
            "    {\n"
            "      \"id\": \"edge_1\",\n"
            "      \"source\": \"node_1\",\n"
            "      \"target\": \"node_2\"\n"
            "    }\n"
            "  ]\n"
            "}"
        )
        mock_chain.invoke.return_value = f"AI Diagnosis result.\n__PIPELINE_DRAFT__:\n{draft_json}"
        mock_get_chain.return_value = mock_chain

        # 确保数据库里至少有一个 ResourcePool 用于 ansible 任务 fallback 寻找
        from apps.host_management.models import ResourcePool
        pool = ResourcePool.objects.create(name="Default Test Pool", code="default_test_pool")

        # 执行自愈诊断任务
        analyze_alert_event(self.alert.id)

        self.alert.refresh_from_db()
        self.assertIsNotNone(self.alert.suggested_pipeline)
        self.assertEqual(self.alert.matched_policy_name, "AI 动态策略 (需确认)")
        self.assertEqual(self.alert.trigger_type, "manual")
        self.assertEqual(self.alert.healing_status, "suggested")

        # 验证流水线图数据
        pipeline = self.alert.suggested_pipeline
        graph = pipeline.graph_data
        nodes = graph.get('nodes', [])
        
        # 1. 验证节点坐标已成功计算，不重叠
        node_1 = next(n for n in nodes if n['id'] == 'node_1')
        node_2 = next(n for n in nodes if n['id'] == 'node_2')
        self.assertIn('position', node_1)
        self.assertIn('position', node_2)
        # 拓扑排序中 node_1 应该在 node_2 的前面
        self.assertLess(node_1['position']['x'], node_2['position']['x'])

        # 2. 验证 Ansible 任务已被自动创建并关联
        ansible_task_id = node_1['data'].get('ansible_task_id')
        self.assertIsNotNone(ansible_task_id)
        from apps.task_management.models import AnsibleTask
        task = AnsibleTask.objects.get(id=ansible_task_id)
        self.assertEqual(task.task_type, "playbook")
        self.assertEqual(task.resource_pool, pool)
        self.assertIn("restart nginx", task.content)

        # 3. 验证 Docker 编译沙箱环境 fallback
        ci_env_id = node_2['data'].get('ci_env_id')
        self.assertIsNotNone(ci_env_id)
        from apps.pipeline_management.models import CIEnvironment
        env_obj = CIEnvironment.objects.get(id=ci_env_id)
        self.assertEqual(env_obj.image, "alpine:latest")

    @patch('apps.ai_engine.rag_service.RAGService.get_chat_chain')
    def test_analyze_alert_event_normalization(self, mock_get_chain):
        """测试 AI 诊断产生的 pipeline 结构规范化（包括 edges from/to 转换、node_type 映射、及 name/content 写入 data）"""
        mock_chain = MagicMock()
        draft_json = (
            "{\n"
            "  \"nodes\": [\n"
            "    {\n"
            "      \"id\": \"check_port\",\n"
            "      \"name\": \"检查端口占用\",\n"
            "      \"type\": \"ansible\",\n"
            "      \"content\": \"ansible.builtin.shell: cmd='ss -tlnp | grep :80'\"\n"
            "    },\n"
            "    {\n"
            "      \"id\": \"confirm_nginx\",\n"
            "      \"name\": \"确认是nginx自身\",\n"
            "      \"type\": \"manual\"\n"
            "    },\n"
            "    {\n"
            "      \"id\": \"notification\",\n"
            "      \"name\": \"通知团队\",\n"
            "      \"type\": \"notification\"\n"
            "    }\n"
            "  ],\n"
            "  \"edges\": [\n"
            "    {\n"
            "      \"from\": \"check_port\",\n"
            "      \"to\": \"confirm_nginx\"\n"
            "    },\n"
            "    {\n"
            "      \"from\": \"confirm_nginx\",\n"
            "      \"to\": \"notification\"\n"
            "    }\n"
            "  ]\n"
            "}"
        )
        mock_chain.invoke.return_value = f"AI Diagnosis result.\n__PIPELINE_DRAFT__:\n{draft_json}"
        mock_get_chain.return_value = mock_chain

        from apps.host_management.models import ResourcePool
        pool = ResourcePool.objects.create(name="Default Test Pool", code="default_test_pool")

        analyze_alert_event(self.alert.id)

        self.alert.refresh_from_db()
        self.assertIsNotNone(self.alert.suggested_pipeline)
        
        pipeline = self.alert.suggested_pipeline
        graph = pipeline.graph_data
        nodes = graph.get('nodes', [])
        edges = graph.get('edges', [])

        # 1. 验证边的 from/to 已被自动规范化为 source/target
        self.assertEqual(len(edges), 2)
        self.assertEqual(edges[0]['source'], 'check_port')
        self.assertEqual(edges[0]['target'], 'confirm_nginx')
        self.assertNotIn('from', edges[0])
        self.assertNotIn('to', edges[0])

        # 2. 验证节点类型映射：manual -> approval, notification -> http_webhook
        node_confirm = next(n for n in nodes if n['id'] == 'confirm_nginx')
        node_notify = next(n for n in nodes if n['id'] == 'notification')
        self.assertEqual(node_confirm['type'], 'approval')
        self.assertEqual(node_notify['type'], 'http_webhook')

        # 3. 验证节点的 label 被成功赋值为 name，且 data 字典不是空的
        for node in nodes:
            self.assertIn('data', node)
            self.assertIsInstance(node['data'], dict)
            self.assertEqual(node['data']['label'], node['name'])

        # 4. 验证 Ansible 模板创建与内容正确提取（即使 content 在 node 根级）
        node_check = next(n for n in nodes if n['id'] == 'check_port')
        ansible_task_id = node_check['data'].get('ansible_task_id')
        self.assertIsNotNone(ansible_task_id)
        from apps.task_management.models import AnsibleTask
        task = AnsibleTask.objects.get(id=ansible_task_id)
        self.assertEqual(task.content, "ansible.builtin.shell: cmd='ss -tlnp | grep :80'")
