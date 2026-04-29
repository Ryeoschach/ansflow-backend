from django.core.management.base import BaseCommand
from apps.rbac_permission.models import Menu, Permission, Role, User

class Command(BaseCommand):
    help = "初始化 RBAC 基础数据：菜单、角色、权限关联"

    def handle(self, *args, **options):
        self.stdout.write("开始初始化 RBAC 数据...")

        # 1. 创建基础菜单
        menus_data = [
            {"title": "仪表盘", "key": "dashboard", "path": "/dashboard", "icon": "DashboardOutlined", "order": 10},
            
            {"title": "主机管理", "key": "host", "path": "/host", "icon": "ServerOutlined", "order": 20, "children": [
                {"title": "资产环境", "key": "host:env", "path": "/host/env", "order": 1},
                {"title": "资源池", "key": "host:pool", "path": "/host/pool", "order": 2},
                {"title": "主机列表", "key": "host:list", "path": "/host/list", "order": 3},
                {"title": "平台接入", "key": "host:platform", "path": "/host/platform", "order": 4},
                {"title": "凭据管理", "key": "host:credential", "path": "/host/credential", "order": 5},
            ]},
            
            {"title": "任务中心", "key": "task", "path": "/task", "icon": "ConsoleSqlOutlined", "order": 30, "children": [
                {"title": "Ansible 任务", "key": "task:ansible", "path": "/task/ansible", "order": 1},
                {"title": "执行历史", "key": "task:history", "path": "/task/history", "order": 2},
                {"title": "周期任务", "key": "task:periodic", "path": "/task/periodic", "order": 3},
            ]},
            
            {"title": "流水线", "key": "pipeline", "path": "/pipeline", "icon": "PartitionOutlined", "order": 40, "children": [
                {"title": "流水线编排", "key": "pipeline:template", "path": "/pipeline/template", "order": 1},
                {"title": "运行记录", "key": "pipeline:run", "path": "/pipeline/run", "order": 2},
                {"title": "CI 环境", "key": "pipeline:env", "path": "/pipeline/env", "order": 3},
            ]},
            
            {"title": "容器化", "key": "k8s", "path": "/k8s", "icon": "ClusterOutlined", "order": 50, "children": [
                {"title": "集群管理", "key": "k8s:cluster", "path": "/k8s/cluster", "order": 1},
                {"title": "Helm 应用", "key": "k8s:helm", "path": "/k8s/helm", "order": 2},
            ]},
            
            {"title": "发布审批", "key": "approval", "path": "/approval", "icon": "CheckCircleOutlined", "order": 60, "children": [
                {"title": "审批中心", "key": "approval:center", "path": "/approval/center", "order": 1},
                {"title": "策略配置", "key": "approval:policy", "path": "/approval/policy", "order": 2},
            ]},
            
            {"title": "系统管理", "key": "system", "path": "/system", "icon": "SettingOutlined", "order": 100, "children": [
                {"title": "用户管理", "key": "system:user", "path": "/system/user", "order": 1},
                {"title": "角色权限", "key": "system:role", "path": "/system/role", "order": 2},
                {"title": "菜单管理", "key": "system:menu", "path": "/system/menu", "order": 3},
                {"title": "配置中心", "key": "system:config", "path": "/system/config", "order": 4},
                {"title": "健康状态", "key": "system:health", "path": "/system/health", "order": 5},
                {"title": "备份恢复", "key": "system:backup", "path": "/system/backup", "order": 6},
                {"title": "审计日志", "key": "system:audit", "path": "/system/audit", "order": 7},
            ]},
        ]

        def create_menus(data, parent=None):
            for item in data:
                children = item.pop("children", [])
                menu, created = Menu.objects.update_or_create(
                    key=item["key"],
                    defaults={**item, "parent": parent}
                )
                if created:
                    self.stdout.write(f"  创建菜单: {menu.title}")
                if children:
                    create_menus(children, parent=menu)

        create_menus(menus_data)

        # 2. 创建超级管理员角色
        admin_role, created = Role.objects.get_or_create(
            code="superuser",
            defaults={"name": "超级管理员"}
        )
        if created:
            self.stdout.write("创建角色: 超级管理员")

        # 3. 为超级管理员分配所有权限和菜单
        all_perms = Permission.objects.all()
        admin_role.permissions.set(all_perms)
        
        all_menus = Menu.objects.all()
        admin_role.menus.set(all_menus)
        self.stdout.write(f"已为超级管理员分配 {all_perms.count()} 个权限和 {all_menus.count()} 个菜单")

        # 4. 将 admin 用户关联到超级管理员角色
        admin_user = User.objects.filter(username="admin").first()
        if admin_user:
            admin_user.roles.add(admin_role)
            self.stdout.write("已将 admin 用户关联至超级管理员角色")
        else:
            self.stdout.write(self.style.WARNING("未找到 admin 用户，请手动关联角色"))

        self.stdout.write(self.style.SUCCESS("RBAC 数据初始化完成！"))
