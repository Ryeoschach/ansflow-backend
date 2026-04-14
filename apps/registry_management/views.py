from rest_framework import viewsets
from rest_framework.filters import SearchFilter, OrderingFilter
from django_filters.rest_framework import DjangoFilterBackend
from .models import ImageRegistry
from .serializers import ImageRegistrySerializer
from utils.rbac_permission import SmartRBACPermission, DataScopeMixin

class ImageRegistryViewSet(DataScopeMixin, viewsets.ModelViewSet):
    """
    镜像仓库管理 API
    """
    queryset = ImageRegistry.objects.all()
    serializer_class = ImageRegistrySerializer
    permission_classes = [SmartRBACPermission]
    resource_type = 'registry'
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    search_fields = ['name', 'url', 'username', 'description']
    ordering_fields = ['create_time', 'update_time']

    resource_code = 'registry:docker'