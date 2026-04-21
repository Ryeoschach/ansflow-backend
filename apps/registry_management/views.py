from rest_framework import viewsets
from rest_framework.filters import SearchFilter, OrderingFilter
from django_filters.rest_framework import DjangoFilterBackend
from .models import ImageRegistry, Artifact, ArtifactVersion
from .serializers import ImageRegistrySerializer, ArtifactSerializer, ArtifactVersionSerializer, ArtifactDetailSerializer
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


class ArtifactViewSet(DataScopeMixin, viewsets.ModelViewSet):
    """
    流水线产物管理 API
    """
    queryset = Artifact.objects.all()
    serializer_class = ArtifactSerializer
    permission_classes = [SmartRBACPermission]
    resource_type = 'artifact'
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    search_fields = ['name', 'description', 'repository']
    ordering_fields = ['create_time', 'update_time', 'name']

    resource_code = 'pipeline:artifact'

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return ArtifactDetailSerializer
        return ArtifactSerializer


class ArtifactVersionViewSet(DataScopeMixin, viewsets.ModelViewSet):
    """
    产物版本管理 API
    """
    queryset = ArtifactVersion.objects.all()
    serializer_class = ArtifactVersionSerializer
    permission_classes = [SmartRBACPermission]
    resource_type = 'artifact'
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['artifact', 'pipeline_run']
    ordering_fields = ['create_time']

    resource_code = 'pipeline:artifact'