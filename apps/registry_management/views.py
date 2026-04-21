from rest_framework import viewsets, permissions
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.filters import SearchFilter, OrderingFilter
from django_filters.rest_framework import DjangoFilterBackend
from .models import ImageRegistry, ArtifactoryInstance, ArtifactoryRepository, Artifact, ArtifactVersion
from .serializers import (
    ImageRegistrySerializer,
    ArtifactoryInstanceSerializer,
    ArtifactoryRepositorySerializer,
    ArtifactSerializer,
    ArtifactVersionSerializer,
    ArtifactDetailSerializer,
)
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


class ArtifactoryInstanceViewSet(DataScopeMixin, viewsets.ModelViewSet):
    """
    Artifactory 实例管理 API
    """
    queryset = ArtifactoryInstance.objects.all()
    serializer_class = ArtifactoryInstanceSerializer
    permission_classes = [SmartRBACPermission]
    resource_type = 'artifactory'
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    search_fields = ['name', 'url', 'username', 'description']
    ordering_fields = ['create_time', 'update_time']
    resource_code = 'registry:artifactory'

    @action(detail=True, methods=['get'])
    def test_connection(self, request, pk=None):
        """测试 Artifactory 连接是否正常"""
        instance = self.get_object()
        try:
            import requests
            resp = requests.get(
                f"{instance.url.rstrip('/')}/api/system/ping",
                auth=instance.get_auth(),
                timeout=5
            )
            if resp.status_code == 200:
                return Response({'status': 'ok', 'message': '连接成功'})
            return Response({'status': 'error', 'message': f'HTTP {resp.status_code}'}, status=503)
        except Exception as e:
            return Response({'status': 'error', 'message': str(e)}, status=503)


class ArtifactoryRepositoryViewSet(DataScopeMixin, viewsets.ModelViewSet):
    """
    Artifactory 仓库管理 API
    """
    queryset = ArtifactoryRepository.objects.all()
    serializer_class = ArtifactoryRepositorySerializer
    permission_classes = [SmartRBACPermission]
    resource_type = 'artifactory'
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['instance', 'repo_type', 'is_active']
    search_fields = ['repo_key', 'description']
    ordering_fields = ['create_time', 'update_time']
    resource_code = 'registry:artifactory'


class ArtifactViewSet(DataScopeMixin, viewsets.ModelViewSet):
    """
    流水线产物管理 API
    """
    queryset = Artifact.objects.all()
    serializer_class = ArtifactSerializer
    permission_classes = [SmartRBACPermission]
    resource_type = 'artifact'
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['source_type', 'type', 'image_registry', 'artifactory_repo', 'pipeline']
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