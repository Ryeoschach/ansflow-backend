from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from apps.rbac_permission.models import User
from apps.pipeline_management.models import Pipeline, PipelineRun, PipelineNodeRun
from apps.pipeline_management.tasks import advance_pipeline_engine, execute_pipeline_node, cleanup_old_workspaces
from unittest.mock import patch, MagicMock
import os
import time
import shutil

class PipelineEngineTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(username='testuser', password='password', email='test@test.com')
        # 创建一个简单的流水线图：node1 -> node2
        self.graph_data = {
            "nodes": [
                {"id": "node1", "type": "input", "data": {"label": "Start"}},
                {"id": "node2", "type": "http_webhook", "data": {"label": "Webhook", "webhook_url": "http://example.com"}},
            ],
            "edges": [
                {"id": "edge1", "source": "node1", "target": "node2"},
            ]
        }
        self.pipeline = Pipeline.objects.create(
            name="Test Pipeline",
            graph_data=self.graph_data,
            creator=self.user
        )

    def test_pipeline_full_success_flow(self):
        """测试完整的流水线成功执行流程"""
        run = PipelineRun.objects.create(pipeline=self.pipeline, trigger_user=self.user, status='pending')
        
        # 1. 模拟启动引擎 - 应该派发 node1 (input 类型)
        with patch('apps.pipeline_management.tasks.execute_pipeline_node.apply_async') as mock_node_exec:
            # 模拟 Celery 的 delay 方法
            with patch('apps.pipeline_management.tasks.advance_pipeline_engine.delay') as mock_advance_delay:
                advance_pipeline_engine(run.id)
            
            # 验证 node1 被下发
            self.assertTrue(mock_node_exec.called)
            # 获取 apply_async 的命名参数中的 args 列表
            _, kwargs = mock_node_exec.call_args
            node_run_id = kwargs['args'][0]
            node_run = PipelineNodeRun.objects.get(id=node_run_id)
            self.assertEqual(node_run.node_id, "node1")
            self.assertEqual(node_run.status, "running")

        # 2. 模拟 node1 执行成功
        node_run.status = 'success'
        node_run.save()
        
        # 3. 再次触发引擎 - 应该派发 node2
        with patch('apps.pipeline_management.tasks.execute_pipeline_node.apply_async') as mock_node_exec:
             # 模拟回调
            with patch('apps.pipeline_management.tasks.advance_pipeline_engine.delay') as mock_advance_delay:
                advance_pipeline_engine(run.id)
            
            self.assertTrue(mock_node_exec.called)
            _, kwargs = mock_node_exec.call_args
            next_node_run_id = kwargs['args'][0]
            next_node_run = PipelineNodeRun.objects.get(id=next_node_run_id)
            self.assertEqual(next_node_run.node_id, "node2")

        # 4. 模拟 node2 执行成功
        next_node_run.status = 'success'
        next_node_run.save()
        
        # 5. 再次触发引擎 - 流水线应标记为成功
        advance_pipeline_engine(run.id)
        run.refresh_from_db()
        self.assertEqual(run.status, 'success')

    def test_node_retry_logic(self):
        """测试节点失败重试逻辑"""
        # 修改图数据，给 node2 增加重试配置
        self.graph_data['nodes'][1]['data']['max_retries'] = 2
        self.graph_data['nodes'][1]['data']['retry_delay'] = 0
        self.pipeline.graph_data = self.graph_data
        self.pipeline.save()
        
        run = PipelineRun.objects.create(pipeline=self.pipeline, trigger_user=self.user, status='running')
        node_run = PipelineNodeRun.objects.create(
            run=run, node_id="node2", node_type="http_webhook", status="pending"
        )

        # 模拟执行节点并失败
        with patch('requests.post', side_effect=Exception("Connection error")):
            with patch('apps.pipeline_management.tasks.execute_pipeline_node.apply_async') as mock_retry:
                with patch('apps.pipeline_management.tasks.advance_pipeline_engine.delay') as mock_advance_delay:
                    execute_pipeline_node(node_run.id)
                
                node_run.refresh_from_db()
                # 状态应该是 running（因为在等待重试），retry_count 应该增加
                self.assertEqual(node_run.retry_count, 1)
                self.assertTrue(mock_retry.called)
                # 验证下一次重试确实被调度了
                self.assertEqual(mock_retry.call_args[1]['countdown'], 0)

    def test_pipeline_cancellation(self):
        """测试流水线取消逻辑"""
        run = PipelineRun.objects.create(pipeline=self.pipeline, trigger_user=self.user, status='cancelled')
        node_run = PipelineNodeRun.objects.create(
            run=run, node_id="node2", node_type="http_webhook", status="pending"
        )

        # 执行节点，它应该在开始前发现 run 已被取消并直接退出
        execute_pipeline_node(node_run.id)
        
        node_run.refresh_from_db()
        self.assertEqual(node_run.status, "failed")
        self.assertIn("流水线已取消", node_run.logs)

    def test_pipeline_approval_flow(self):
        """测试审批节点的暂停与恢复逻辑"""
        approval_graph = {
            "nodes": [
                {"id": "node1", "type": "input", "data": {"label": "Start"}},
                {"id": "node2", "type": "approval", "data": {"label": "Manual Approval"}},
                {"id": "node3", "type": "http_webhook", "data": {"label": "Finish"}},
            ],
            "edges": [
                {"id": "e1", "source": "node1", "target": "node2"},
                {"id": "e2", "source": "node2", "target": "node3"},
            ]
        }
        self.pipeline.graph_data = approval_graph
        self.pipeline.save()

        run = PipelineRun.objects.create(pipeline=self.pipeline, trigger_user=self.user, status='running')
        PipelineNodeRun.objects.create(run=run, node_id="node1", node_type="input", status="success")

        # 1. 触发引擎 - 应该下发 node2 (Approval)
        with patch('apps.pipeline_management.tasks.push_pipeline_status_to_ws'):
            advance_pipeline_engine(run.id)
        
        node2_run = PipelineNodeRun.objects.get(run=run, node_id="node2")
        self.assertEqual(node2_run.status, "waiting") # 验证引擎成功拦截并置为等待

        # 2. 模拟调用 API 通过审批
        from rest_framework.test import APIClient
        client = APIClient()
        client.force_authenticate(user=self.user)
        url = reverse('pipeline_node_runs-approve', args=[node2_run.id])
        
        with patch('apps.pipeline_management.tasks.advance_pipeline_engine.delay') as mock_advance:
            response = client.post(url, {"action": "pass", "comment": "All good"})
            self.assertEqual(response.status_code, 200)
            
            node2_run.refresh_from_db()
            self.assertEqual(node2_run.status, "success")
            self.assertEqual(node2_run.approver, self.user)
            self.assertTrue(mock_advance.called) # 验证引擎被重新唤醒

    def test_node_workspace_isolation(self):
        """测试并行的节点是否拥有独立的工作目录"""
        run = PipelineRun.objects.create(pipeline=self.pipeline, trigger_user=self.user, status='running')
        node_run = PipelineNodeRun.objects.create(
            run=run, node_id="test_node_99", node_type="input", status="pending"
        )
        
        with patch('apps.pipeline_management.tasks.push_pipeline_status_to_ws'):
            execute_pipeline_node(node_run.id)
            
        # 验证目录是否存在
        node_workspace = f"/tmp/ansflow_workspaces/run_{run.id}/node_test_node_99"
        self.assertTrue(os.path.exists(node_workspace))
        
        # 清理
        shutil.rmtree(f"/tmp/ansflow_workspaces/run_{run.id}", ignore_errors=True)

    def test_pipeline_variable_resolution(self):
        """测试流水线变量引用逻辑"""
        from .utils import resolve_pipeline_vars
        
        run = PipelineRun.objects.create(pipeline=self.pipeline, trigger_user=self.user, status='running')
        # 模拟上游节点产出了变量
        PipelineNodeRun.objects.create(
            run=run, node_id="node1", node_type="kaniko_build", 
            status="success", output_data={"tag": "v1.2.3", "image": "my-app"}
        )

        # 待解析的配置数据
        raw_data = {
            "image_tag": "{{ nodes.node1.tag }}",
            "full_name": "{{ nodes.node1.image }}:{{ nodes.node1.tag }}",
            "constant": "static-val",
            "nested": {
                "ref": "{{ nodes.node1.tag }}"
            }
        }

        resolved = resolve_pipeline_vars(raw_data, run.id)

        self.assertEqual(resolved["image_tag"], "v1.2.3")
        self.assertEqual(resolved["full_name"], "my-app:v1.2.3")
        self.assertEqual(resolved["constant"], "static-val")
        self.assertEqual(resolved["nested"]["ref"], "v1.2.3")

    def test_ansible_node_execution_variables(self):
        """测试 Ansible 节点执行完毕后，能够正确填充 stdout, rc 等输出变量以供后续节点引用"""
        from apps.task_management.models import AnsibleTask, AnsibleExecution, TaskLog
        from apps.host_management.models import ResourcePool
        
        # 1. 创建资源池与 Ansible 任务
        pool = ResourcePool.objects.create(name="Test Pool", code="test_pool")
        ansible_task = AnsibleTask.objects.create(
            name="Test Task",
            task_type="playbook",
            resource_pool=pool,
            content="---",
            creator=self.user
        )
        
        # 2. 修改图数据，包含一个 ansible 类型的节点
        self.graph_data = {
            "nodes": [
                {"id": "check_port", "type": "ansible", "data": {"label": "Check Port", "ansible_task_id": ansible_task.id}},
            ],
            "edges": []
        }
        self.pipeline.graph_data = self.graph_data
        self.pipeline.save()
        
        run = PipelineRun.objects.create(pipeline=self.pipeline, trigger_user=self.user, status='running')
        node_run = PipelineNodeRun.objects.create(
            run=run, node_id="check_port", node_type="ansible", status="pending"
        )
        
        # 3. 模拟 run_ansible_task 执行，当执行时，我们在数据库中模拟产生 TaskLog 日志
        def mock_run_ansible_task(execution_id, extra_vars=None):
            exec_obj = AnsibleExecution.objects.get(id=execution_id)
            exec_obj.status = 'success'
            exec_obj.save()
            # 写入日志
            TaskLog.objects.create(execution=exec_obj, host="server-01", output="Port 80 is listening")
            TaskLog.objects.create(execution=exec_obj, host="SYSTEM", output="Ignored system log")
            return {'status': 'success', 'logs': 'Execution finished successfully'}
            
        with patch('apps.task_management.tasks.run_ansible_task', side_effect=mock_run_ansible_task):
            with patch('apps.pipeline_management.tasks.push_pipeline_status_to_ws'):
                execute_pipeline_node(node_run.id)
                
        node_run.refresh_from_db()
        self.assertEqual(node_run.status, "success")
        # 验证 output_data 包含了 stdout / rc / output / status
        self.assertIn("stdout", node_run.output_data)
        self.assertEqual(node_run.output_data["stdout"], "Port 80 is listening")
        self.assertEqual(node_run.output_data["rc"], 0)
        self.assertEqual(node_run.output_data["status"], "success")
        
        # 4. 验证变量解析
        from .utils import resolve_pipeline_vars
        raw_data = {
            "cmd_output": "{{ nodes.check_port.stdout }}",
            "return_code": "{{ nodes.check_port.rc }}"
        }
        resolved = resolve_pipeline_vars(raw_data, run.id)
        self.assertEqual(resolved["cmd_output"], "Port 80 is listening")
        self.assertEqual(resolved["return_code"], "0")

    def test_dag_deadlock_detection_concept(self):
        """测试循环依赖导致的死锁（验证现状）"""
        circular_graph = {
            "nodes": [
                {"id": "node1", "type": "docker_build", "data": {"label": "B1"}},
                {"id": "node2", "type": "docker_build", "data": {"label": "B2"}},
            ],
            "edges": [
                {"id": "e1", "source": "node1", "target": "node2"},
                {"id": "e2", "source": "node2", "target": "node1"},
            ]
        }
        self.pipeline.graph_data = circular_graph
        self.pipeline.save()
        
        run = PipelineRun.objects.create(pipeline=self.pipeline, trigger_user=self.user, status='pending')
        
        # 启动引擎
        with patch('apps.pipeline_management.tasks.advance_pipeline_engine.delay'):
            advance_pipeline_engine(run.id)
        
        run.refresh_from_db()
        self.assertEqual(run.status, 'running')
        self.assertEqual(run.nodes.filter(status='pending').count(), 2)

    def test_workspace_cleanup(self):
        """测试工作空间清理任务"""
        base_dir = "/tmp/ansflow_workspaces"
        os.makedirs(base_dir, exist_ok=True)
        
        # 1. 创建一个“过期”目录 (run_999)
        expired_dir = os.path.join(base_dir, "run_999")
        os.makedirs(expired_dir, exist_ok=True)
        
        # 2. 创建一个“新”目录 (run_888)
        new_dir = os.path.join(base_dir, "run_888")
        os.makedirs(new_dir, exist_ok=True)
        
        # 模拟修改时间：run_999 为 2 天前，run_888 为现在
        two_days_ago = time.time() - (2 * 86400)
        os.utime(expired_dir, (two_days_ago, two_days_ago))
        
        # 执行清理任务，清理 1 天前的
        result = cleanup_old_workspaces(days=1)
        
        self.assertIn("Cleaned up", result)
        self.assertFalse(os.path.exists(expired_dir))
        self.assertTrue(os.path.exists(new_dir))
        
        # 清理测试现场
        shutil.rmtree(new_dir, ignore_errors=True)
