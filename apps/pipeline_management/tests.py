from django.test import TestCase
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
        self.user = User.objects.create_user(username='testuser', password='password')
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
