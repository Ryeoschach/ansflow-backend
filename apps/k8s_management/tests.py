from django.test import TestCase
from unittest.mock import patch, MagicMock
from django.utils import timezone
from .models import K8sCluster, HelmRepository
from .tasks import sync_k8s_cluster_status
from .utils.helm_runner import run_helm_upgrade

class K8sHelmTestCase(TestCase):
    def setUp(self):
        # 1. 创建模拟集群
        self.cluster = K8sCluster.objects.create(
            name="Test Cluster",
            auth_type="token",
            api_server="https://k8s.example.com",
            token="fake-token"
        )
        
        # 2. 创建模拟 Helm 仓库
        self.repo = HelmRepository.objects.create(
            name="Stable Repo",
            url="https://charts.helm.sh/stable",
            username="admin",
            password="password123"
        )

    @patch('apps.k8s_management.tasks.get_k8s_client')
    @patch('kubernetes.client.VersionApi')
    @patch('kubernetes.client.CoreV1Api')
    def test_sync_cluster_status(self, mock_core_api, mock_version_api, mock_get_client):
        """测试集群健康状态同步任务"""
        # 模拟 ApiClient
        mock_get_client.return_value = MagicMock()
        
        # 模拟版本信息
        mock_v_info = MagicMock()
        mock_v_info.major = "1"
        mock_v_info.minor = "28"
        mock_version_api.return_value.get_code.return_value = mock_v_info
        
        # 模拟节点信息
        mock_node = MagicMock()
        mock_node.status.conditions = [MagicMock(type='Ready', status='True')]
        mock_node.status.capacity = {'cpu': '4', 'memory': '8Gi'}
        
        mock_nodes_list = MagicMock()
        mock_nodes_list.items = [mock_node]
        mock_core_api.return_value.list_node.return_value = mock_nodes_list

        # 执行同步任务
        sync_k8s_cluster_status(self.cluster.id)
        
        # 验证结果
        self.cluster.refresh_from_db()
        self.assertEqual(self.cluster.status, 'running')
        self.assertEqual(self.cluster.version, 'v1.28')
        self.assertEqual(self.cluster.node_count, 1)
        self.assertEqual(self.cluster.ready_node_count, 1)
        self.assertIn('4', self.cluster.cpu_capacity)
        self.assertIn('8.0 GiB', self.cluster.memory_capacity)

    @patch('subprocess.run')
    def test_helm_runner_remote_pull(self, mock_run):
        """测试 Helm 运行器的远程拉取逻辑"""
        # 模拟 subprocess 成功返回
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Release updated"
        mock_run.return_value = mock_result

        # 执行部署
        success, output = run_helm_upgrade(
            self.cluster, 
            name="my-app", 
            chart="nginx",
            repo_url=self.repo.url,
            repo_auth={'username': self.repo.username, 'password': self.repo.password}
        )

        self.assertTrue(success)
        # 验证 subprocess 至少被调用了 3 次 (repo add, repo update, upgrade)
        self.assertTrue(mock_run.call_count >= 3)
        
        # 验证第一次调用包含 repo add 和认证信息
        first_args = mock_run.call_args_list[0][0][0]
        self.assertIn('repo', first_args)
        self.assertIn('add', first_args)
        self.assertIn('admin', first_args)
        self.assertIn('password123', first_args)

    @patch('apps.k8s_management.utils.helm_runner.run_helm_upgrade')
    def test_pipeline_integration(self, mock_helm_runner):
        """测试流水线节点与 Helm 增强逻辑的集成"""
        from apps.pipeline_management.models import Pipeline, PipelineRun, PipelineNodeRun
        from apps.pipeline_management.tasks import execute_pipeline_node
        from django.contrib.auth import get_user_model
        
        User = get_user_model()
        user = User.objects.create_user(username='testrunner', password='pwd')
        
        pipeline = Pipeline.objects.create(name="Helm Pipeline", creator=user)
        run = PipelineRun.objects.create(pipeline=pipeline, trigger_user=user, status='running')
        
        # 构造一个配置了仓库 ID 的节点数据
        node_data = {
            "k8s_cluster_id": self.cluster.id,
            "k8s_release_name": "release-01",
            "k8s_chart_name": "redis",
            "k8s_repo_id": self.repo.id,
            "k8s_values": "replicaCount: 3"
        }
        
        pipeline.graph_data = {
            "nodes": [{"id": "n1", "type": "k8s_deploy", "data": node_data}],
            "edges": []
        }
        pipeline.save()
        
        node_run = PipelineNodeRun.objects.create(
            run=run, node_id="n1", node_type="k8s_deploy", status="pending"
        )

        mock_helm_runner.return_value = (True, "Success")
        
        with patch('apps.pipeline_management.tasks.push_pipeline_status_to_ws'):
             execute_pipeline_node(node_run.id)
        
        # 验证调用时是否带了仓库 URL
        mock_helm_runner.assert_called_once()
        kwargs = mock_helm_runner.call_args[1]
        self.assertEqual(kwargs['repo_url'], self.repo.url)
        self.assertEqual(kwargs['repo_auth']['username'], 'admin')
        self.assertIn('replicaCount: 3', kwargs['values'])
