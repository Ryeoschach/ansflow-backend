from drf_spectacular.openapi import AutoSchema
from utils.rbac_permission import SmartRBACPermission, RBAC_ACTION_MAP

class RBACAutoSchema(AutoSchema):
    TAG_BY_RESOURCE_CODE = {
        'credential:ssh_credentials': 'SSH 凭据',
        'resource:resource_pools': '资源池管理',
        'resource:baselines': '合规基线',
        'resource:compliance': '合规基线',
        'tasks:ansible_tasks': 'Ansible 任务',
        'tasks:ansible_executions': 'Ansible 任务',
        'tasks:ansible_schedules': 'Ansible 任务',
        'pipeline:artifact': '镜像与制品',
        'registry:artifactory': 'Artifactory 制品库',
        'system:credential': '凭据保险库',
        'system:approval_policy': '审批中心',
        'system:approval_resource': '审批中心',
        'system:approval_ticket': '审批中心',
        'system:periodic_tasks': '系统定时任务',
        'task:pulse': 'Task Pulse 监控',
        'task:worker': 'Task Pulse 监控',
    }

    TAG_BY_RESOURCE_CODE_PREFIX = {
        'ai': 'AI 与知识库',
        'rbac': 'RBAC 权限管理',
        'resource': '主机资源管理',
        'credential': 'SSH 凭据',
        'tasks': 'Ansible 任务',
        'ansible': 'Ansible 任务',
        'k8s': 'Kubernetes 管理',
        'helm': 'Helm 仓库',
        'pipeline': '流水线管理',
        'registry': '镜像与制品',
        'config': '配置中心',
        'sre': 'SRE 告警自愈',
        'task': 'Task Pulse 监控',
    }

    TAG_BY_RESOURCE_TYPE = {
        'ai': 'AI 与知识库',
        'credential': 'SSH 凭据',
        'ansible_task': 'Ansible 任务',
        'ansible_schedule': 'Ansible 任务',
        'k8s': 'Kubernetes 管理',
        'k8s_cluster': 'Kubernetes 管理',
        'pipeline': '流水线管理',
        'registry': '镜像与制品',
        'artifactory': 'Artifactory 制品库',
        'artifact': '镜像与制品',
        'resource_pool': '资源池管理',
        'sre': 'SRE 告警自愈',
    }

    TAG_BY_VIEW_NAME = {
        'ProjectViewSet': '项目管理',
        'ProjectMemberViewSet': '项目管理',
        'ProjectAssetShareViewSet': '项目资产共享',
        'SshCredentialViewSet': 'SSH 凭据',
        'HostViewSet': '主机资源管理',
        'EnvironmentViewSet': '主机资源管理',
        'PlatformViewSet': '主机资源管理',
        'ResourcePoolViewSet': '资源池管理',
        'HostBaselineViewSet': '合规基线',
        'ComplianceFrameworkViewSet': '合规基线',
        'ComplianceClauseViewSet': '合规基线',
        'ComplianceBaselineMappingViewSet': '合规基线',
        'ArtifactoryInstanceViewSet': 'Artifactory 制品库',
        'ArtifactoryRepositoryViewSet': 'Artifactory 制品库',
        'ImageRegistryViewSet': '镜像与制品',
        'ArtifactViewSet': '镜像与制品',
        'ArtifactVersionViewSet': '镜像与制品',
        'SystemHealthViewSet': '系统管理',
        'DashboardViewSet': '系统仪表盘',
        'BackupViewSet': '系统备份恢复',
        'PeriodicTaskViewSet': '系统定时任务',
        'UserNotificationViewSet': '系统通知',
        'SystemReportViewSet': '系统报表',
        'CredentialViewSet': '凭据保险库',
        'ApprovalPolicyViewSet': '审批中心',
        'ApprovalResourceViewSet': '审批中心',
        'ApprovalTicketViewSet': '审批中心',
        'TaskPulseViewSet': 'Task Pulse 监控',
        'WorkerNodeViewSet': 'Task Pulse 监控',
    }

    TAG_BY_PATH_PREFIX = [
        ('/api/v1/auth/', '认证'),
        ('/api/v1/account/', '账号中心'),
        ('/api/v1/system/backup', '系统备份恢复'),
        ('/api/v1/system/dashboard', '系统仪表盘'),
        ('/api/v1/system/health', '系统管理'),
        ('/api/v1/system/notifications', '系统通知'),
        ('/api/v1/system/periodic-tasks', '系统定时任务'),
        ('/api/v1/system/reports', '系统报表'),
        ('/api/v1/system/', '系统管理'),
        ('/api/v1/config/', '配置中心'),
        ('/api/v1/approval_', '审批中心'),
        ('/api/v1/pulse/', 'Task Pulse 监控'),
        ('/api/v1/sre/', 'SRE 告警自愈'),
        ('/api/v1/ai/', 'AI 与知识库'),
        ('/api/v1/pipeline/', '流水线管理'),
        ('/api/v1/pipelines', '流水线管理'),
        ('/api/v1/pipeline_runs', '流水线管理'),
        ('/api/v1/pipeline_node_runs', '流水线管理'),
        ('/api/v1/ci_environments', '流水线管理'),
        ('/api/v1/tasks', 'Ansible 任务'),
        ('/api/v1/executions', 'Ansible 任务'),
        ('/api/v1/schedules', 'Ansible 任务'),
        ('/api/v1/k8s', 'Kubernetes 管理'),
        ('/api/v1/helm_repositories', 'Helm 仓库'),
        ('/api/v1/hosts', '主机资源管理'),
        ('/api/v1/environments', '主机资源管理'),
        ('/api/v1/platforms', '主机资源管理'),
        ('/api/v1/resource_pools', '资源池管理'),
        ('/api/v1/host_baselines', '合规基线'),
        ('/api/v1/compliance/', '合规基线'),
        ('/api/v1/ssh_credentials', 'SSH 凭据'),
        ('/api/v1/image_registries', '镜像与制品'),
        ('/api/v1/artifacts', '镜像与制品'),
        ('/api/v1/artifactory/', 'Artifactory 制品库'),
        ('/api/v1/credentials', '凭据保险库'),
        ('/api/v1/users', 'RBAC 权限管理'),
        ('/api/v1/roles', 'RBAC 权限管理'),
        ('/api/v1/audit-logs', 'RBAC 权限管理'),
        ('/api/v1/projects', '项目管理'),
        ('/api/v1/project-members', '项目管理'),
        ('/api/v1/asset-shares', '项目资产共享'),
    ]

    def get_tags(self):
        original_tags = super().get_tags()
        default_tags = self._tokenize_path()[:1]
        if original_tags and original_tags != default_tags:
            return original_tags

        explicit_tags = getattr(self.view, 'schema_tags', None)
        if explicit_tags:
            return explicit_tags

        resource_code = getattr(self.view, 'resource_code', None)
        if resource_code:
            tag = self.TAG_BY_RESOURCE_CODE.get(resource_code)
            if tag:
                return [tag]
            prefix = resource_code.split(':', 1)[0]
            tag = self.TAG_BY_RESOURCE_CODE_PREFIX.get(prefix)
            if tag:
                return [tag]

        resource_type = getattr(self.view, 'resource_type', None)
        tag = self.TAG_BY_RESOURCE_TYPE.get(resource_type)
        if tag:
            return [tag]

        view_name = self.view.__class__.__name__
        tag = self.TAG_BY_VIEW_NAME.get(view_name)
        if tag:
            return [tag]

        path = getattr(self, 'path', '')
        for prefix, tag in self.TAG_BY_PATH_PREFIX:
            if path.startswith(prefix):
                return [tag]

        return original_tags

    def get_description(self):
        # 获取原有的描述（从 @extend_schema 注解来的）
        description = super().get_description() or ""

        # 判断当前视图是否使用了 SmartRBACPermission
        permissions = getattr(self.view, 'permission_classes', [])
        if SmartRBACPermission in permissions:
            # 尝试提取资源码
            resource = getattr(self.view, 'resource_code', None)
            if resource:
                # 获取当前请求动作，如果在 ViewSet 中通常是 action，否则是 method
                # drf_spectacular 处理 method 时直接使用 self.method
                action = getattr(self.view, 'action', None) or self.method.lower()

                # RBAC 权限映射表
                perm_action = RBAC_ACTION_MAP.get(action, action)

                # perm_action = action_map.get(action, action)
                if not perm_action:
                    perm_action = 'unknown'

                required_code = f"{resource}:{perm_action}"

                # 将所需权限自动拼接进文档描述结尾
                rbac_notice = f"\n\n> **[RBAC 权限]**: 需拥有 `{required_code}` (或对应继承/通配符权限) 才能访问。"
                description += rbac_notice

        return description
