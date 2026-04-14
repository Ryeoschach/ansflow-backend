from rest_framework import viewsets
from ..models import K8sCluster
from ..serializers import K8sClusterSerializer
from utils.rbac_permission import SmartRBACPermission, DataScopeMixin

from .k8s_views import K8sManagementMixin
from .helm_views import HelmManagementMixin

class K8sClusterViewSet(DataScopeMixin, viewsets.ModelViewSet, K8sManagementMixin, HelmManagementMixin):
    """
    聚合后的 K8s 集群管理视图
    """
    queryset = K8sCluster.objects.all()
    serializer_class = K8sClusterSerializer
    permission_classes = [SmartRBACPermission]
    resource_type = 'k8s_cluster'

    # 声明权限标识列表，供权限同步脚本 (sync_perms) 使用
    resource_codes = ['k8s:cluster', 'helm:chart']

    @property
    def resource_code(self):
        """
        动态切换权限码。
        SmartRBACPermission 会通过 getattr 访问此属性。
        """
        # 如果是 helm_ 开头或 chart_ 开头的方法，判定为 Helm 权限
        if hasattr(self, 'action') and self.action:
            if self.action.startswith(('helm_', 'chart_')) or self.action == 'helm_list':
                return 'helm:chart'
        # 其余判定为 K8s 基础资源权限
        return 'k8s:cluster'
