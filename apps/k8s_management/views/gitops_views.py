from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from apps.k8s_management.models import K8sApplication
from apps.k8s_management.serializers import K8sApplicationSerializer
from utils.rbac_permission import SmartRBACPermission, DataScopeMixin
from apps.k8s_management.tasks import sync_k8s_application

class K8sApplicationViewSet(DataScopeMixin, viewsets.ModelViewSet):
    """
    K8s GitOps 应用管理
    """
    queryset = K8sApplication.objects.all()
    serializer_class = K8sApplicationSerializer
    permission_classes = [SmartRBACPermission]
    resource_code = 'k8s:application'
    resource_type = 'k8s'
    
    @action(detail=True, methods=['post'])
    def sync(self, request, pk=None):
        """
        手动触发同步/漂移检测
        """
        app = self.get_object()
        sync_k8s_application.delay(app.id)
        return Response({"message": "同步任务已下发"})
