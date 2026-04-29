from django.core.management.base import BaseCommand
from apps.rbac_permission.models import Menu, Permission, Role, User

class Command(BaseCommand):
    help = "初始化 RBAC 基础数据：菜单、角色、权限关联"

    def handle(self, *args, **options):
        self.stdout.write("开始初始化 RBAC 数据...")

        # 1. 基础菜单数据 (同步自 db.sqlite3)
        menus_data = [
            {"title": "Dashboard", "title_en": "", "key": "dashboard", "path": "v1/dashboard", "icon": "DashboardOutlined", "order": 0},
            {"title": "状态监控", "title_en": "Monitoring", "key": "system-monitor", "path": "v1/system/monitor", "icon": "HeartOutlined", "order": 1},
            {"title": "Ansible任务", "title_en": "Ansible Management", "key": "ansible", "path": "ansible", "icon": "carbon:logo-red-hat-ansible", "order": 2, "children": [
                {"title": "Ansible模版", "title_en": "Ansble Template", "key": "ansible_template", "path": "v1/task/ansible", "icon": "gg:template", "order": 0},
                {"title": "Ansible执行历史", "title_en": "Ansible History", "key": "ansible_history", "path": "v1/task/executions", "icon": "icon-park-outline:history-query", "order": 1},
                {"title": "ansible调度", "title_en": "Ansible Schedules", "key": "ansible-schedules", "path": "/v1/task/schedules", "icon": "hugeicons:time-schedule", "order": 2},
            ]},
            {"title": "编排流水线", "title_en": "Pipeline Edit", "key": "pipeline", "path": "Pipelines", "icon": "carbon:pipelines", "order": 3, "children": [
                {"title": "流水线列表", "title_en": "Pipeline List", "key": "pipeline_list", "path": "v1/pipeline/list", "icon": "hugeicons:timeline-list", "order": 0},
                {"title": "流水线配置", "title_en": "Pipeline Design", "key": "pipeline_designer", "path": "v1/pipeline/designer", "icon": "f7:hand-draw", "order": 1},
                {"title": "制品管理", "title_en": "Artifacts", "key": "artifacts", "path": "v1/pipeline/artifacts", "icon": "carbon:repo-artifact", "order": 2},
                {"title": "Webhook触发器", "title_en": "Webhook", "key": "webhook", "path": "v1/pipeline/webhooks", "icon": "mingcute:webhook-line", "order": 3},
            ]},
            {"title": "容器配置", "title_en": "Container Center", "key": "ContainerCenter", "path": "ContainerCenter", "icon": "carbon:web-services-container", "order": 4, "children": [
                {"title": "构建镜像", "title_en": "Builder Container", "key": "CIEnvironments", "path": "v1/ci-envs", "icon": "streamline-logos:docker-logo", "order": 0},
            ]},
            {"title": "K8S中心", "title_en": "K8S Center", "key": "k8s", "path": "k8smanagement", "icon": "ant-design:kubernetes-outlined", "order": 5, "children": [
                {"title": "k8s集群管理", "title_en": "k8s Management", "key": "cluster", "path": "v1/k8s/management", "icon": "carbon:kubernetes-worker-node", "order": 0},
                {"title": "helm管理", "title_en": "helm Management", "key": "helm", "path": "v1/k8s/helm", "icon": "simple-icons:helm", "order": 1},
            ]},
            {"title": "操作审计", "title_en": "Audit Center", "key": "AuditLog", "path": "v1/system/audit-logs", "icon": "hugeicons:audit-01", "order": 6},
            {"title": "审批表", "title_en": "Approvals", "key": "approvals", "path": "v1/system/approvals", "icon": "fluent:approvals-app-48-filled", "order": 7},
            {"title": "资源管理", "title_en": "Resources Management", "key": "resources", "path": "resources", "icon": "grommet-icons:resources", "order": 888, "children": [
                {"title": "平台管理", "title_en": "Platform Management", "key": "platform", "path": "v1/system/platforms", "icon": "tdesign:control-platform", "order": 0},
                {"title": "环境管理", "title_en": "Envs Management", "key": "envs", "path": "v1/system/envs", "icon": "fluent-mdl2:server-enviroment", "order": 1},
                {"title": "资源池", "title_en": "Resource Pool", "key": "resourcepool", "path": "v1/system/resourcepool", "icon": "clarity:resource-pool-outline-alerted", "order": 2},
                {"title": "主机管理", "title_en": "Host Management", "key": "hosts", "path": "v1/system/hosts", "icon": "material-symbols-light:host-outline", "order": 3},
                {"title": "SSH 凭据", "title_en": "Credentials Management", "key": "credentials", "path": "v1/system/credentials", "icon": "KeyOutlined", "order": 50},
            ]},
            {"title": "配置中心", "title_en": "Config Center", "key": "config center", "path": "v1/system/config", "icon": "carbon:document-configuration", "order": 889},
            {"title": "系统管理", "title_en": "System Management", "key": "system", "path": "system", "icon": "SettingOutlined", "order": 999, "children": [
                {"title": "菜单管理", "title_en": "Menu Management", "key": "menu", "path": "v1/system/menus", "icon": "MenuOutlined", "order": 0},
                {"title": "用户管理", "title_en": "User Management", "key": "users", "path": "v1/system/users", "icon": "UserOutlined", "order": 1},
                {"title": "权限管理", "title_en": "Permission Management", "key": "permissions", "path": "v1/system/permissions", "icon": "SafetyCertificateOutlined", "order": 2},
                {"title": "角色管理", "title_en": "Roles Management", "key": "roles", "path": "v1/system/roles", "icon": "TeamOutlined", "order": 3},
                {"title": "系统备份/还原", "title_en": "System Backup", "key": "backup", "path": "/v1/system/backup", "icon": "iconoir:database-backup", "order": 4},
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
