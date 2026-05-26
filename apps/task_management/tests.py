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

from rest_framework.test import APITestCase
from django.contrib.auth import get_user_model
from apps.rbac_permission.models import Role, DataPolicy, Permission

from django.core.cache import cache

class TaskApiTest(APITestCase):
    def setUp(self):
        cache.clear()
        User = get_user_model()
        self.user = User.objects.create_user(username='testuser', password='password')
        self.other_user = User.objects.create_user(username='otheruser', password='password')
        self.role = Role.objects.create(name="Tester", code="tester")
        
        # 创建并分配功能权限以通过 SmartRBAC 校验
        p1 = Permission.objects.create(name='View Task', code='tasks:ansible_tasks:view', module='tasks')
        p2 = Permission.objects.create(name='View Execution', code='tasks:ansible_executions:view', module='tasks')
        self.role.permissions.add(p1, p2)
        
        self.user.roles.add(self.role)
        self.client.force_authenticate(user=self.user)



        self.env = Environment.objects.create(name="Prod", code="prod")
        self.pool = ResourcePool.objects.create(name="Web Pool", code="web_pool")
        self.pool.hosts.create(hostname="web-01", env=self.env)

        # 创建一个普通任务模板和一个系统任务模板
        self.normal_task = AnsibleTask.objects.create(
            name="Normal Task",
            task_type="cmd",
            resource_pool=self.pool,
            content="echo normal",
            creator=self.user,
            create_type="manual"
        )
        self.system_task = AnsibleTask.objects.create(
            name="System Task",
            task_type="playbook",
            resource_pool=self.pool,
            content="echo system",
            creator=None,
            create_type="system"
        )

        # 为正常模板和系统模板各创建一条执行记录
        self.normal_exec = AnsibleExecution.objects.create(
            task=self.normal_task,
            status='success',
            executor=self.other_user
        )
        self.system_exec = AnsibleExecution.objects.create(
            task=self.system_task,
            status='success',
            executor=None
        )

    def test_ansible_task_list_excludes_system_tasks(self):
        """测试任务模板列表接口是否自动排除了系统级临时任务"""
        # 首先配置数据策略允许访问 ansible_task
        DataPolicy.objects.create(
            role=self.role,
            resource_type="ansible_task",
            action_type="use",
            authorized_ids=["*"]
        )

        response = self.client.get('/api/v1/tasks/')
        self.assertEqual(response.status_code, 200)
        
        data = response.json()
        task_names = [t['name'] for t in data['data']]
        self.assertIn("Normal Task", task_names)
        self.assertNotIn("System Task", task_names)

    def test_system_task_execution_visibility_via_pool_permission(self):
        """测试系统任务的执行记录对于拥有其所属资源池权限的用户是否可见"""
        # 情况 1：无任何策略，正常执行记录和系统执行记录都查不到 (常规执行历史没配权限，系统执行没配 pool 权限)
        response = self.client.get('/api/v1/executions/')
        self.assertEqual(response.status_code, 200)
        
        data = response.json()
        exec_ids = [e['id'] for e in data['data']]
        self.assertNotIn(self.normal_exec.id, exec_ids)
        self.assertNotIn(self.system_exec.id, exec_ids)

        # 情况 2：仅配置常规任务的权限，则能查到 normal_exec，但查不到 system_exec
        DataPolicy.objects.create(
            role=self.role,
            resource_type="ansible_task",
            action_type="use",
            authorized_ids=[self.normal_task.id]
        )
        from django.core.cache import cache
        cache.clear()

        response = self.client.get('/api/v1/executions/')
        self.assertEqual(response.status_code, 200)
        
        data = response.json()
        exec_ids = [e['id'] for e in data['data']]
        self.assertIn(self.normal_exec.id, exec_ids)
        self.assertNotIn(self.system_exec.id, exec_ids)

        # 情况 3：加配资源池的 view 权限，虽然还是没有 system_task 的权限，但是应该能查到 system_exec
        DataPolicy.objects.create(
            role=self.role,
            resource_type="resource_pool",
            action_type="view",
            authorized_ids=[self.pool.id]
        )
        cache.clear()

        response = self.client.get('/api/v1/executions/')
        self.assertEqual(response.status_code, 200)
        
        data = response.json()
        exec_ids = [e['id'] for e in data['data']]
        self.assertIn(self.normal_exec.id, exec_ids)
        self.assertIn(self.system_exec.id, exec_ids)


