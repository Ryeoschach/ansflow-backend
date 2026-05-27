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
            "      \"content\": \"- name: check port\\n  hosts: '{{ instance }}'\\n  tasks:\\n    - name: check\\n      shell: ss -tlnp | grep :80\"\n"
            "    },\n"
            "    {\n"
            "      \"id\": \"clone_code\",\n"
            "      \"name\": \"拉取最新代码\",\n"
            "      \"type\": \"git\",\n"
            "      \"data\": {\n"
            "        \"git_repo\": \"https://github.com/foo/bar.git\"\n"
            "      }\n"
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
            "    },\n"
            "    {\n"
            "      \"id\": \"approval_by_name\",\n"
            "      \"name\": \"人工审批修复\",\n"
            "      \"type\": \"ansible\"\n"
            "    }\n"
            "  ],\n"
            "  \"edges\": [\n"
            "    {\n"
            "      \"from\": \"check_port\",\n"
            "      \"to\": \"clone_code\"\n"
            "    },\n"
            "    {\n"
            "      \"from\": \"clone_code\",\n"
            "      \"to\": \"confirm_nginx\"\n"
            "    },\n"
            "    {\n"
            "      \"from\": \"confirm_nginx\",\n"
            "      \"to\": \"notification\"\n"
            "    },\n"
            "    {\n"
            "      \"from\": \"notification\",\n"
            "      \"to\": \"approval_by_name\"\n"
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

        # 1. 验证边 from/to 规范化，且由于过滤节点，边已旁路缝合
        # 过滤了 confirm_nginx (type: manual -> approval) 和 approval_by_name (name: 人工审批修复)
        # check_port -> clone_code (保留)
        # clone_code -> confirm_nginx -> notification 缝合为 clone_code -> notification
        # notification -> approval_by_name (approval_by_name无流出，直接被裁剪)
        # 因此，最终剩下的边是: check_port -> clone_code, clone_code -> notification (共2条)
        self.assertEqual(len(edges), 2)
        edge_1 = next(e for e in edges if e['source'] == 'check_port')
        self.assertEqual(edge_1['target'], 'clone_code')
        self.assertNotIn('from', edge_1)
        self.assertNotIn('to', edge_1)

        edge_2 = next(e for e in edges if e['source'] == 'clone_code')
        self.assertEqual(edge_2['target'], 'notification')

        # 2. 验证非动作节点已被过滤 (confirm_nginx 被过滤，approval_by_name 因名称被过滤)
        self.assertFalse(any(n['id'] == 'confirm_nginx' for n in nodes))
        self.assertFalse(any(n['id'] == 'approval_by_name' for n in nodes))

        # 3. 验证节点类型映射：git -> git_clone, notification -> http_webhook
        node_clone = next(n for n in nodes if n['id'] == 'clone_code')
        node_notify = next(n for n in nodes if n['id'] == 'notification')
        self.assertEqual(node_clone['type'], 'git_clone')
        self.assertEqual(node_notify['type'], 'http_webhook')

        # 4. 验证节点的 label 被成功赋值为 name，且 data 字典不是空的
        for node in nodes:
            self.assertIn('data', node)
            self.assertIsInstance(node['data'], dict)
            self.assertEqual(node['data']['label'], node['name'])

        # 5. 验证 Ansible 模板创建与内容正确提取（即使 content 在 node 根级）
        node_check = next(n for n in nodes if n['id'] == 'check_port')
        ansible_task_id = node_check['data'].get('ansible_task_id')
        self.assertIsNotNone(ansible_task_id)
        from apps.task_management.models import AnsibleTask
        task = AnsibleTask.objects.get(id=ansible_task_id)
        self.assertIn("hosts: all", task.content)
        self.assertNotIn("hosts: '{{ instance }}'", task.content)

    @patch('apps.ai_engine.rag_service.RAGService.get_chat_chain')
    def test_analyze_alert_event_nonstandard_nodes_and_filename_translation(self, mock_get_chain):
        """测试 AI 诊断产生的非标准节点映射、各字段提取、非标准节点过滤旁路及文件名智能转换"""
        mock_chain = MagicMock()
        draft_json = (
            "{\n"
            "  \"nodes\": [\n"
            "    {\n"
            "      \"id\": \"start_node\",\n"
            "      \"name\": \"告警触发\",\n"
            "      \"type\": \"trigger\"\n"
            "    },\n"
            "    {\n"
            "      \"id\": \"check_port\",\n"
            "      \"name\": \"检查端口占用\",\n"
            "      \"type\": \"task\",\n"
            "      \"data\": {\n"
            "        \"ansible_playbook\": \"check_port_80.yml\"\n"
            "      }\n"
            "    },\n"
            "    {\n"
            "      \"id\": \"approval_node\",\n"
            "      \"name\": \"人工确认\",\n"
            "      \"type\": \"manual\"\n"
            "    },\n"
            "    {\n"
            "      \"id\": \"fix_port\",\n"
            "      \"name\": \"自愈解决占用\",\n"
            "      \"type\": \"execute\",\n"
            "      \"exec\": \"fix_port_80.yml\"\n"
            "    },\n"
            "    {\n"
            "      \"id\": \"disk_diag\",\n"
            "      \"name\": \"磁盘占用诊断\",\n"
            "      \"type\": \"shell\",\n"
            "      \"data\": {\n"
            "        \"script\": \"check_disk.yml\"\n"
            "      }\n"
            "    }\n"
            "  ],\n"
            "  \"edges\": [\n"
            "    {\"source\": \"start_node\", \"target\": \"check_port\"},\n"
            "    {\"source\": \"check_port\", \"target\": \"approval_node\"},\n"
            "    {\"source\": \"approval_node\", \"target\": \"fix_port\"},\n"
            "    {\"source\": \"fix_port\", \"target\": \"disk_diag\"}\n"
            "  ]\n"
            "}"
        )
        mock_chain.invoke.return_value = f"AI Diagnosis result.\n__PIPELINE_DRAFT__:\n{draft_json}"
        mock_get_chain.return_value = mock_chain

        from apps.host_management.models import ResourcePool
        pool = ResourcePool.objects.create(name="Default Test Pool 2", code="default_test_pool_2")

        analyze_alert_event(self.alert.id)

        self.alert.refresh_from_db()
        self.assertIsNotNone(self.alert.suggested_pipeline)
        
        pipeline = self.alert.suggested_pipeline
        graph = pipeline.graph_data
        nodes = graph.get('nodes', [])
        edges = graph.get('edges', [])

        # 1. 验证非标准节点（trigger和manual）已被完全过滤剔除
        self.assertFalse(any(n['id'] == 'start_node' for n in nodes))
        self.assertFalse(any(n['id'] == 'approval_node' for n in nodes))
        
        # 验证仅留下三个动作节点
        self.assertEqual(len(nodes), 3)
        node_check = next(n for n in nodes if n['id'] == 'check_port')
        node_fix = next(n for n in nodes if n['id'] == 'fix_port')
        node_disk = next(n for n in nodes if n['id'] == 'disk_diag')

        self.assertEqual(node_check['type'], 'ansible')
        self.assertEqual(node_fix['type'], 'ansible')
        self.assertEqual(node_disk['type'], 'ansible')

        # 验证边关系已正确旁路缝合：
        # start_node 被裁掉，因此 check_port 成为起点
        # approval_node 在 check_port 和 fix_port 之间被裁掉并旁路，因此应该产生 check_port -> fix_port 边
        self.assertEqual(len(edges), 2)
        edge_1 = next(e for e in edges if e['source'] == 'check_port')
        self.assertEqual(edge_1['target'], 'fix_port')
        edge_2 = next(e for e in edges if e['source'] == 'fix_port')
        self.assertEqual(edge_2['target'], 'disk_diag')

        # 2. 验证 check_port_80.yml 翻译为实际的检测 wait_for playbook，且类型为 playbook
        ansible_task_id = node_check['data'].get('ansible_task_id')
        self.assertIsNotNone(ansible_task_id)
        from apps.task_management.models import AnsibleTask
        task = AnsibleTask.objects.get(id=ansible_task_id)
        self.assertEqual(task.task_type, "playbook")
        self.assertIn("Wait for port 80 to be open", task.content)
        self.assertIn("wait_for", task.content)

        # 3. 验证 fix_port_80.yml (从根级的 exec 提取) 被翻译为 kill 进程的 playbook，且类型为 playbook
        ansible_task_id_fix = node_fix['data'].get('ansible_task_id')
        self.assertIsNotNone(ansible_task_id_fix)
        task_fix = AnsibleTask.objects.get(id=ansible_task_id_fix)
        self.assertEqual(task_fix.task_type, "playbook")
        self.assertIn("Kill processes using port 80", task_fix.content)
        self.assertIn("shell: lsof -t -i:80 | xargs -r kill -9", task_fix.content)

        # 4. 验证 check_disk.yml 被翻译为 df -h 的 playbook
        ansible_task_id_disk = node_disk['data'].get('ansible_task_id')
        self.assertIsNotNone(ansible_task_id_disk)
        task_disk = AnsibleTask.objects.get(id=ansible_task_id_disk)
        self.assertEqual(task_disk.task_type, "playbook")
        self.assertIn("Check Disk Usage", task_disk.content)
        self.assertIn("shell: df -h", task_disk.content)

    @patch('apps.ai_engine.rag_service.ChatOpenAI.invoke')
    def test_rag_service_refine_dag(self, mock_invoke):
        """测试 RAGService.refine_dag 方法能正确构造执行链并调用 LLM"""
        from langchain_core.messages import AIMessage
        mock_invoke.return_value = AIMessage(content="refined response")

        from apps.ai_engine.rag_service import RAGService
        rag = RAGService()

        nodes = [{"id": "n1", "type": "ansible", "name": "test"}]
        edges = []
        result = rag.refine_dag(prompt_text="add a git clone step", nodes=nodes, edges=edges)
        
        self.assertEqual(result, "refined response")
        self.assertTrue(mock_invoke.called)

    @patch('apps.ai_engine.rag_service.RAGService.get_chat_chain')
    @patch('apps.sre_management.tasks.trigger_self_healing.delay')
    def test_self_healing_circuit_breaker(self, mock_trigger, mock_get_chain):
        """测试自愈动态熔断机制"""
        # 1. 模拟 LLM 决策链
        mock_chain = MagicMock()
        mock_chain.invoke.return_value = "AI Analysis recommendation."
        mock_get_chain.return_value = mock_chain

        # 2. 创建自愈策略，设置为自动执行 (is_auto_execute=True)
        policy = SelfHealingPolicy.objects.create(
            name="CPU High Autopilot",
            alert_match_rule={"host": "server-01"},
            pipeline=self.pipeline,
            is_auto_execute=True,
            is_active=True
        )

        # 3. 模拟此前 1 小时内已发生了 3 次同指纹的自动自愈记录
        for i in range(3):
            AlertEvent.objects.create(
                alert_name="High CPU Usage",
                severity="critical",
                status="firing",
                fingerprint="abc-123",
                labels={"host": "server-01"},
                trigger_type="auto",
                healing_status="success",
                suggested_pipeline=self.pipeline
            )

        # 4. 再次触发同一指纹的告警分析 (即第 4 次触发)
        analyze_alert_event(self.alert.id)

        # 验证结果
        self.alert.refresh_from_db()
        # 4.1 验证状态是否被强制设置为待审批 awaiting_approval
        self.assertEqual(self.alert.healing_status, "awaiting_approval")
        # 4.2 验证触发方式已从自动 auto 降级为手动 manual
        self.assertEqual(self.alert.trigger_type, "manual")
        # 4.3 验证 AI 结论中是否包含熔断警告字样
        self.assertIn("熔断保护已触发", self.alert.ai_analysis)
        # 4.4 验证没有再次调用 trigger_self_healing.delay() 触发执行 (被拦截)
        self.assertFalse(mock_trigger.called)


from django.urls import reverse
from rest_framework import status
from apps.config_center.models import ConfigItem, ConfigCategory

class AlertWebhookAuthTestCase(TestCase):
    def setUp(self):
        self.category, _ = ConfigCategory.objects.get_or_create(
            name='notification',
            defaults={'label': 'Notification', 'description': 'desc'}
        )
        self.webhook_url = reverse('sre-alerts-receive')
        
    def tearDown(self):
        ConfigItem.objects.filter(category=self.category, key='webhook_token').delete()
        from utils.config_manager import ConfigCache
        ConfigCache.invalidate('notification', 'webhook_token')

    def _set_token(self, value):
        ConfigItem.objects.update_or_create(
            category=self.category,
            key='webhook_token',
            defaults={'value': value, 'value_type': 'string'}
        )
        from utils.config_manager import ConfigCache
        ConfigCache.invalidate('notification', 'webhook_token')

    def test_no_token_configured_allows_all(self):
        """当未配置 Token 时（或配置为空），接口允许任意访问"""
        self._set_token('')
        
        payload = {"alerts": []}
        response = self.client.post(self.webhook_url, payload, content_type='application/json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_configured_token_without_token_rejected(self):
        """当配置了 Token 但请求没有携带任何 Token，接口返回 403"""
        self._set_token('secret-token-123')
        
        payload = {"alerts": []}
        response = self.client.post(self.webhook_url, payload, content_type='application/json')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_configured_token_with_wrong_token_rejected(self):
        """当提供了错误的 Token，接口返回 403"""
        self._set_token('secret-token-123')
        
        payload = {"alerts": []}
        # Bearer Token 错误
        response = self.client.post(
            self.webhook_url, 
            payload, 
            content_type='application/json',
            HTTP_AUTHORIZATION='Bearer wrong-token'
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        
        # URL token 错误
        url_with_wrong_token = f"{self.webhook_url}?token=wrong-token"
        response = self.client.post(url_with_wrong_token, payload, content_type='application/json')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_configured_token_with_correct_bearer_token_allowed(self):
        """当提供了正确的 Bearer Token，接口返回 200"""
        self._set_token('secret-token-123')
        
        payload = {"alerts": []}
        response = self.client.post(
            self.webhook_url, 
            payload, 
            content_type='application/json',
            HTTP_AUTHORIZATION='Bearer secret-token-123'
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_configured_token_with_correct_url_token_allowed(self):
        """当提供了正确的 URL Query token，接口返回 200"""
        self._set_token('secret-token-123')
        
        payload = {"alerts": []}
        url_with_correct_token = f"{self.webhook_url}?token=secret-token-123"
        response = self.client.post(url_with_correct_token, payload, content_type='application/json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class AlertWebhookNotificationTestCase(TestCase):
    def setUp(self):
        self.webhook_url = reverse('sre-alerts-receive')
        
    @patch('apps.system_management.notifiers.notify_alert_firing')
    @patch('apps.system_management.notifiers.notify_alert_resolved')
    def test_alert_notification_on_transition(self, mock_resolved, mock_firing):
        """测试告警状态改变时是否正确触发了通知"""
        # 1. 首次收到 firing 告警 -> 触发 notify_alert_firing
        payload_firing = {
            "alerts": [
                {
                    "fingerprint": "alert-test-999",
                    "status": "firing",
                    "labels": {"alertname": "TestNotifyAlert", "severity": "warning"},
                    "annotations": {"description": "test notification"}
                }
            ]
        }
        response = self.client.post(self.webhook_url, payload_firing, content_type='application/json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_firing.assert_called_once()
        mock_resolved.assert_not_called()
        
        mock_firing.reset_mock()
        mock_resolved.reset_mock()
        
        # 2. 再次收到同一 firing 告警 -> 不触发 notify_alert_firing
        response = self.client.post(self.webhook_url, payload_firing, content_type='application/json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_firing.assert_not_called()
        mock_resolved.assert_not_called()
        
        # 3. 收到 resolved 告警 -> 触发 notify_alert_resolved
        payload_resolved = {
            "alerts": [
                {
                    "fingerprint": "alert-test-999",
                    "status": "resolved",
                    "labels": {"alertname": "TestNotifyAlert", "severity": "warning"},
                    "annotations": {"description": "test notification"}
                }
            ]
        }
        response = self.client.post(self.webhook_url, payload_resolved, content_type='application/json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_firing.assert_not_called()
        mock_resolved.assert_called_once()

    @patch('apps.sre_management.tasks.analyze_alert_event.delay')
    def test_alert_ignored_by_config(self, mock_delay):
        """测试如果告警名称被配置在忽略列表中，是否跳过了 AI 分析并设置为 ignored 状态"""
        # 1. 设置忽略的告警名称
        from apps.config_center.models import ConfigCategory, ConfigItem
        from utils.config_manager import ConfigCache
        
        category, _ = ConfigCategory.objects.get_or_create(
            name='sre',
            defaults={'label': 'SRE Config', 'description': 'desc'}
        )
        ConfigItem.objects.update_or_create(
            category=category,
            key='sre.ignored_alert_names',
            defaults={'value': 'IgnoredCPUAlert,AnotherAlert', 'value_type': 'string'}
        )
        ConfigCache.invalidate('sre', 'sre.ignored_alert_names')
        
        # 2. 发送一个被忽略名称的告警
        payload = {
            "alerts": [
                {
                    "fingerprint": "alert-test-888",
                    "status": "firing",
                    "labels": {"alertname": "IgnoredCPUAlert", "severity": "warning"},
                    "annotations": {"description": "test notification"}
                }
            ]
        }
        response = self.client.post(self.webhook_url, payload, content_type='application/json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        # 3. 验证没有触发 AI 诊断分析
        mock_delay.assert_not_called()
        
        # 4. 验证在数据库中的自愈状态为 'ignored'
        alert_obj = AlertEvent.objects.get(fingerprint="alert-test-888")
        self.assertEqual(alert_obj.healing_status, 'ignored')
        
        # 5. 清理
        ConfigItem.objects.filter(category=category, key='sre.ignored_alert_names').delete()
        ConfigCache.invalidate('sre', 'sre.ignored_alert_names')



