from django.apps import AppConfig


class K8SManagementConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.k8s_management'
    verbose_name = 'K8s 容器管理'

    def ready(self):
        from apps.approval_center.registry import approval_registry
        approval_registry.register(
            code="k8s:helm_install",
            name="Helm 应用安装/升级 (K8s 变更拦截)",
            name_en="Helm Install/Upgrade (K8s Change Intercept)",
            icon="ClusterOutlined",
            description="针对 K8s 集群中 Helm Chart 的安装、升级或回滚操作进行拦截。",
            description_en="Intercept Helm Chart installation, upgrade, or rollback operations in the K8s cluster."
        )
