from django.test import TestCase
from apps.host_management.models import Environment, Host, ResourcePool, Platform
from apps.task_management.models import AnsibleTask, AnsibleExecution
from apps.task_management.utils import generate_ansible_inventory
from apps.task_management.tasks import run_ansible_task
from unittest.mock import patch, MagicMock

class TaskManagementTest(TestCase):
    def setUp(self):
        self.env = Environment.objects.create(name="Prod", code="prod")
        self.platform = Platform.objects.create(name="Aliyun", type="aliyun")
        self.host = Host.objects.create(
            hostname="web-01", 
            private_ip="10.0.0.1", 
            env=self.env, 
            platform=self.platform,
            os_type="Ubuntu 22.04",
            cpu=2,
            memory=4
        )
        self.pool = ResourcePool.objects.create(name="Web Pool", code="web_pool")
        self.pool.hosts.add(self.host)
        
        self.task = AnsibleTask.objects.create(
            name="Test Task",
            task_type="cmd",
            resource_pool=self.pool,
            content="echo hello",
            forks=10
        )

    def test_inventory_metadata_injection(self):
        """测试 Inventory 中是否正确注入了主机元数据"""
        inventory = generate_ansible_inventory(self.pool.id)
        
        group_key = f"pool_{self.pool.code}"
        host_vars = inventory["all"]["children"][group_key]["hosts"]["web-01"]
        
        self.assertEqual(host_vars["node_env_code"], "prod")
        self.assertEqual(host_vars["node_os_type"], "Ubuntu 22.04")
        self.assertEqual(host_vars["node_cpu"], 2)
        self.assertEqual(host_vars["node_platform"], "Aliyun")

    @patch('ansible_runner.run')
    def test_run_ansible_task_forks(self, mock_run):
        """测试执行任务时是否正确传递了 forks 参数"""
        # 模拟 runner 返回
        mock_result = MagicMock()
        mock_result.rc = 0
        mock_result.stats = {}
        mock_run.return_value = mock_result
        
        execution = AnsibleExecution.objects.create(task=self.task, status='pending')
        
        run_ansible_task(execution.id, extra_vars={"foo": "bar"})
        
        # 检查参数
        args, kwargs = mock_run.call_args
        self.assertEqual(kwargs['forks'], 10)
        self.assertEqual(kwargs['extravars'], {"foo": "bar"})
        
        # 检查变量快照是否保存（注意：快照是在 ViewSet 的 action 中保存的，Task 内部不保存快照，只读取）
        # 这里验证 Task 能够正常运行即可
