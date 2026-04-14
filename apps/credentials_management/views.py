from rest_framework import viewsets
from .models import Credential
from .serializers import CredentialSerializer
from utils.rbac_permission import SmartRBACPermission

class CredentialViewSet(viewsets.ModelViewSet):
    """
    全量统一凭据管理视图
    """
    queryset = Credential.objects.all().order_by('-create_time')
    serializer_class = CredentialSerializer
    filterset_fields = ['type', 'name']
    
    permission_classes = [SmartRBACPermission]
    resource_code = 'system:credential'
    permission_labels = {
        'view': {'name': '查看凭据令牌清单'},
        'add': {'name': '新增敏感凭据', 'danger': 'warn'},
        'edit': {'name': '修改凭据配置', 'danger': 'warn'},
        'delete': {'name': '删除系统凭据', 'danger': 'high'},
    }
