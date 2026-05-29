from django.core.management.base import BaseCommand
from apps.rbac_permission.models import Menu, Permission, Role, User
import os

class Command(BaseCommand):
    help = "初始化/重置 RBAC 基础数据及系统默认配置"

    def add_arguments(self, parser):
        parser.add_argument(
            '--force',
            action='store_true',
            help='强制覆盖已存在的菜单属性（如标题、图标、路径、排序）',
        )

    def init_ai_settings(self):
        """从环境变量同步初始 AI 配置到数据库"""
        try:
            from apps.ai_engine.models import AIProvider, AIModel, AIConfig
        except ImportError:
            self.stdout.write(self.style.WARNING("AI 引擎模块未找到，跳过 AI 配置初始化。"))
            return

        api_key = os.getenv("LLM_API_KEY")
        base_url = os.getenv("LLM_API_BASE", "https://api.deepseek.com")
        
        if not api_key:
            self.stdout.write("未检测到 LLM_API_KEY 环境变量，跳过 AI 供应商初始化。")
            return

        # 1. 创建/获取 LLM 供应商 (从环境变量)
        provider_name = "DeepSeek" if "deepseek" in base_url.lower() else "Default Provider"
        provider_type = "deepseek" if "deepseek" in base_url.lower() else "other"
        
        provider = AIProvider.objects.filter(provider_type=provider_type).first()
        created = False
        if not provider:
            provider = AIProvider.objects.create(
                provider_type=provider_type,
                name=provider_name,
                base_url=base_url,
                api_key=api_key,
                is_active=True
            )
            created = True
            self.stdout.write(self.style.SUCCESS(f"创建默认 LLM 供应商: {provider_name}"))
        elif not provider.api_key:
             provider.api_key = api_key
             provider.save()

        # 2. 创建/获取本地供应商 (用于 Embedding 和 Rerank)
        local_provider = AIProvider.objects.filter(provider_type="local").first()
        if not local_provider:
            local_provider = AIProvider.objects.create(
                provider_type="local",
                name="Local Model Service",
                is_active=True
            )

        # 3. 创建分析模型 (LLM)
        llm_model_name = os.getenv("LLM_MODEL_NAME", "deepseek-chat")
        llm, _ = AIModel.objects.get_or_create(
            provider=provider,
            name=llm_model_name,
            model_type="llm",
            defaults={"display_name": f"{provider_name} - {llm_model_name}"}
        )

        # 4. 创建向量模型 (Embedding)
        # 默认优先使用本地模型，除非明确配置了外部 API
        emb_api_key = os.getenv("EMBEDDING_API_KEY")
        emb_model_name = os.getenv("EMBEDDING_MODEL_NAME", "BAAI/bge-small-zh-v1.5")
        
        emb_provider = provider if (emb_api_key and provider_type != "local") else local_provider
        embedding, _ = AIModel.objects.get_or_create(
            provider=emb_provider,
            name=emb_model_name,
            model_type="embedding",
            defaults={"display_name": f"Local - {emb_model_name}" if emb_provider == local_provider else emb_model_name}
        )

        # 5. 创建重排序模型 (Rerank)
        # 默认优先使用本地模型，除非明确配置了外部 API
        rerank_api_base = os.getenv("RERANK_API_BASE")
        rerank_model_name = os.getenv("RERANK_MODEL_NAME", "ms-marco-MiniLM-L-12-v2")
        
        rerank_provider = provider if rerank_api_base else local_provider
        rerank, _ = AIModel.objects.get_or_create(
            provider=rerank_provider,
            name=rerank_model_name,
            model_type="rerank",
            defaults={"display_name": f"Remote - {rerank_model_name}" if rerank_provider == provider else f"Local - {rerank_model_name}"}
        )

        # 6. 初始化全局配置
        ai_config, created = AIConfig.objects.get_or_create(
            name="default",
            defaults={
                "default_llm": llm,
                "default_embedding": embedding,
                "default_rerank": rerank,
                "rag_top_k": 5,
                "rag_score_threshold": 0.4
            }
        )
        if created:
            self.stdout.write(self.style.SUCCESS("初始化全局 AI 配置成功"))
        else:
            update_fields = []
            if not ai_config.default_llm:
                ai_config.default_llm = llm
                update_fields.append("default_llm")
            if not ai_config.default_embedding:
                ai_config.default_embedding = embedding
                update_fields.append("default_embedding")
            if not ai_config.default_rerank:
                ai_config.default_rerank = rerank
                update_fields.append("default_rerank")
            
            if update_fields:
                ai_config.save(update_fields=update_fields)
                self.stdout.write(f"更新全局 AI 配置字段: {', '.join(update_fields)}")

    def handle(self, *args, **options):
        force = options['force']
        self.stdout.write(f"开始同步系统基础数据 (强制模式: {'开启' if force else '关闭'})...")

        # 1. 同步 AI 配置 (从环境变量)
        self.init_ai_settings()

        # 2. 深度嵌套的菜单树
        menus_tree = [
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
                {"title": "制品库", "title_en": "registries", "key": "registries", "path": "v1/pipeline/registries", "icon": "mdi:locker-multiple", "order": 1},
            ]},
            {"title": "K8S中心", "title_en": "K8S Center", "key": "k8s", "path": "k8smanagement", "icon": "ant-design:kubernetes-outlined", "order": 5, "children": [
                {"title": "k8s集群管理", "title_en": "k8s Management", "key": "cluster", "path": "v1/k8s/management", "icon": "carbon:kubernetes-worker-node", "order": 0},
                {"title": "helm管理", "title_en": "helm Management", "key": "helm", "path": "v1/k8s/helm", "icon": "simple-icons:helm", "order": 1},
                {"title": "GitOps 应用", "title_en": "GitOps", "key": "GitOps", "path": "v1/k8s/gitops", "icon": "arcticons:gitnex", "order": 2},
            ]},
            {"title": "操作审计", "title_en": "Audit Center", "key": "AuditLog", "path": "v1/system/audit-logs", "icon": "hugeicons:audit-01", "order": 6},
            {"title": "审批表", "title_en": "Approvals", "key": "approvals", "path": "v1/system/approvals", "icon": "fluent:approvals-app-48-filled", "order": 7},
            {"title": "SRE运维", "title_en": "SRE", "key": "sre", "path": "sre", "icon": "carbon:uv-index-alt", "order": 8, "children": [
                {"title": "告警中心", "title_en": "Alert Center", "key": "sre-alerts", "path": "v1/sre/alerts", "icon": "carbon:alarm", "order": 0},
                {"title": "任务脉搏", "title_en": "Task Pulse", "key": "sre-pulse", "path": "v1/sre/pulse", "icon": "carbon:activity", "order": 1},
                {"title": "告警报表", "title_en": "Alert Report", "key": "sre-report", "path": "v1/sre/report", "icon": "carbon:report", "order": 2},
            ]},
            {"title": "AI与RAG配置", "title_en": "Config AI&RAG", "key": "configAI", "path": "v1/ai-rag/config", "icon": "arcticons:ai-chat-open-assistant-chatbot", "order": 9},
            {"title": "等保2.0", "title_en": "compliance2.0", "key": "compliance", "path": "v1/system/compliance", "icon": "carbon:ibm-cloud-security-compliance-center", "order": 10},
            {"title": "报表中心", "title_en": "Report Center", "key": "system-reports", "path": "v1/system/reports", "icon": "carbon:analytics", "order": 88},
            {"title": "资源管理", "title_en": "Resources Management", "key": "resources", "path": "resources", "icon": "grommet-icons:resources", "order": 888, "children": [
                {"title": "平台管理", "title_en": "Platform Management", "key": "platform", "path": "v1/system/platforms", "icon": "tdesign:control-platform", "order": 0},
                {"title": "环境管理", "title_en": "Envs Management", "key": "envs", "path": "v1/system/envs", "icon": "fluent-mdl2:server-enviroment", "order": 1},
                {"title": "资源池", "title_en": "Resource Pool", "key": "resourcepool", "path": "v1/system/resourcepool", "icon": "clarity:resource-pool-outline-alerted", "order": 2},
                {"title": "主机管理", "title_en": "Host Management", "key": "hosts", "path": "v1/system/hosts", "icon": "material-symbols-light:host-outline", "order": 3},
                {"title": "主机基线", "title_en": "host baselines", "key": "hostBaselines", "path": "v1/system/host-baselines", "icon": "arcticons:hostelworld", "order": 4},
                {"title": "SSH 凭据", "title_en": "Credentials Management", "key": "credentials", "path": "v1/system/credentials", "icon": "KeyOutlined", "order": 50},
            ]},
            {"title": "配置中心", "title_en": "Config Center", "key": "config center", "path": "v1/system/config", "icon": "carbon:document-configuration", "order": 889},
            {"title": "系统管理", "title_en": "System Management", "key": "system", "path": "system", "icon": "SettingOutlined", "order": 999, "children": [
                {"title": "菜单管理", "title_en": "Menu Management", "key": "menu", "path": "v1/system/menus", "icon": "MenuOutlined", "order": 0},
                {"title": "用户管理", "title_en": "User Management", "key": "users", "path": "v1/system/users", "icon": "UserOutlined", "order": 1},
                {"title": "权限管理", "title_en": "Permission Management", "key": "permissions", "path": "v1/system/permissions", "icon": "SafetyCertificateOutlined", "order": 2},
                {"title": "角色管理", "title_en": "Roles Management", "key": "roles", "path": "v1/system/roles", "icon": "TeamOutlined", "order": 3},
                {"title": "系统备份/还原", "title_en": "System Backup", "key": "backup", "path": "/v1/system/backups", "icon": "iconoir:database-backup", "order": 4},
                {"title": "定时任务", "title_en": "Scheduled Tasks", "key": "periodic-tasks", "path": "/v1/system/periodic-tasks", "icon": "ant-design:schedule-outlined", "order": 5},
                {"title": "项目管理", "title_en": "Project Management", "key": "projects", "path": "v1/system/projects", "icon": "ProjectOutlined", "order": 6},
                {"title": "跨项目授权", "title_en": "Cross-project Authorization", "key": "asset-shares", "path": "v1/system/asset-shares", "icon": "ShareAltOutlined", "order": 7},
            ]},
        ]

        def sync_menus(data, parent=None):
            for item in data:
                children = item.pop("children", [])
                key = item["key"]
                
                # 查找是否已存在
                menu = Menu.objects.filter(key=key).first()
                
                if menu:
                    if force:
                        # 强制覆盖模式：更新所有属性
                        for field, value in item.items():
                            setattr(menu, field, value)
                        menu.parent = parent
                        menu.save()
                        self.stdout.write(f"  [OVERWRITE] 菜单: {menu.title} ({key})")
                    else:
                        self.stdout.write(f"  [SKIP] 菜单已存在: {menu.title} ({key})")
                else:
                    # 不存在则创建
                    menu = Menu.objects.create(**item, parent=parent)
                    self.stdout.write(self.style.SUCCESS(f"  [NEW] 创建菜单: {menu.title} ({key})"))
                
                if children:
                    sync_menus(children, parent=menu)

        # 执行同步
        sync_menus(menus_tree)

        # 2. 角色同步
        admin_role, created = Role.objects.get_or_create(
            code="superuser",
            defaults={"name": "超级管理员"}
        )
        if created:
            self.stdout.write(self.style.SUCCESS("创建角色: 超级管理员"))

        # 3. 关联全量资源 (总是同步以确保管理员有权)
        all_perms = Permission.objects.all()
        admin_role.permissions.set(all_perms)
        
        all_menus = Menu.objects.all()
        admin_role.menus.set(all_menus)
        self.stdout.write(f"已确保超级管理员角色拥有全量权限({all_perms.count()})和菜单({all_menus.count()})")

        # 4. 用户关联
        admin_user = User.objects.filter(username="admin").first()
        if admin_user:
            if not admin_user.roles.filter(code="superuser").exists():
                admin_user.roles.add(admin_role)
                self.stdout.write(self.style.SUCCESS("已将 admin 用户提升为超级管理员角色"))
        
        self.stdout.write(self.style.SUCCESS("RBAC 数据同步完成！"))
