from rest_framework import viewsets, status
from rest_framework.response import Response
import paramiko
import io

from apps.host_management.models import (
    Host, Environment, ResourcePool, Platform, SshCredential, HostBaseline,
    ComplianceFramework, ComplianceClause, ComplianceBaselineMapping
)

from apps.host_management.serializers import (
    HostSerializer, EnvironmentSerializer, PlatformSerializer,
    ResourceSerializer, SshCredentialSerializer, HostBaselineSerializer,
    ComplianceFrameworkSerializer, ComplianceClauseSerializer, ComplianceBaselineMappingSerializer
)
from utils.rbac_permission import SmartRBACPermission, DataScopeMixin
from rest_framework.decorators import action


class SshCredentialViewSet(DataScopeMixin, viewsets.ModelViewSet):
    queryset = SshCredential.objects.all()
    serializer_class = SshCredentialSerializer
    permission_classes = [SmartRBACPermission]
    resource_type = 'credential'

    resource_code = 'credential:ssh_credentials'
    asset_share_type = 'ssh_credential'

    def perform_create(self, serializer):
        serializer.save(project=getattr(self.request, 'project', None))

    @action(detail=True, methods=['post'])
    def verify(self, request, pk=None):
        """
        测试凭据是否能成功连接到指定主机
        """
        credential = self.get_object()
        target_host = request.data.get('host')
        target_port = int(request.data.get('port', 22))

        if not target_host:
            return Response({"error": "请提供测试目标主机 IP"}, status=status.HTTP_400_BAD_REQUEST)

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
        try:
            if credential.auth_type == 'password':
                client.connect(
                    hostname=target_host,
                    port=target_port,
                    username=credential.username,
                    password=credential.password,
                    timeout=10
                )
            else:
                # 处理私钥
                key_stream = io.StringIO(credential.private_key)
                if 'BEGIN RSA PRIVATE KEY' in credential.private_key:
                    pkey = paramiko.RSAKey.from_private_key(key_stream, password=credential.passphrase)
                elif 'BEGIN OPENSSH PRIVATE KEY' in credential.private_key:
                    pkey = paramiko.Ed25519Key.from_private_key(key_stream, password=credential.passphrase)
                else:
                    # 尝试自动识别
                    pkey = paramiko.RSAKey.from_private_key(key_stream, password=credential.passphrase)
                
                client.connect(
                    hostname=target_host,
                    port=target_port,
                    username=credential.username,
                    pkey=pkey,
                    timeout=10
                )
            
            client.close()
            return Response({"status": "success", "message": "连接验证成功"})
            
        except Exception as e:
            return Response({"status": "failed", "message": f"连接失败: {str(e)}"}, status=status.HTTP_200_OK)


from apps.host_management.filters import HostFilter, ResourcePoolFilter
from apps.host_management.tasks import verify_platform_connectivity, sync_platform_assets, check_host_baseline


class HostBaselineViewSet(DataScopeMixin, viewsets.ModelViewSet):
    """
    主机基线管理
    """
    queryset = HostBaseline.objects.all()
    serializer_class = HostBaselineSerializer
    permission_classes = [SmartRBACPermission]
    resource_code = 'resource:baselines'
    resource_type = 'resource_pool'
    resource_lookup_field = 'resource_pool_id'

    def get_queryset(self):
        queryset = super().get_queryset()
        project = getattr(self.request, 'project', None)
        if project:
            queryset = queryset.filter(resource_pool__project=project)
        return queryset

    @action(detail=True, methods=['post'])
    def check(self, request, pk=None):
        """
        手动触发基线巡检
        """
        baseline = self.get_object()
        check_host_baseline.delay(baseline.id)
        return Response({"message": "基线巡检任务已下发"})




class HostViewSet(DataScopeMixin, viewsets.ModelViewSet):
    queryset = Host.objects.all()
    serializer_class = HostSerializer
    permission_classes = [SmartRBACPermission]
    filterset_class = HostFilter

    resource_code = 'resource:hosts'
    asset_share_type = 'host'

    @action(detail=False, methods=['post'])
    def bulk_import(self, request):
        """
        批量导入主机
        格式要求: [{"hostname": "xxx", "private_ip": "1.1.1.1", "env": 1, ...}]
        """
        data = request.data
        if not isinstance(data, list):
            return Response({"error": "数据格式错误，期望收到列表格式"}, status=status.HTTP_400_BAD_REQUEST)

        success_count = 0
        errors = []

        for index, item in enumerate(data):
            try:
                # 使用 Serializer 验证单条数据
                serializer = self.get_serializer(data=item)
                serializer.is_valid(raise_exception=True)
                serializer.save(project=getattr(request, 'project', None))
                success_count += 1
            except Exception as e:
                errors.append(f"第 {index+1} 条记录错误: {str(e)}")

        return Response({
            "status": "success",
            "message": f"成功导入 {success_count} 台主机",
            "errors": errors
        })

    def perform_create(self, serializer):
        serializer.save(project=getattr(self.request, 'project', None))

class EnvironmentViewSet(viewsets.ModelViewSet):
    queryset = Environment.objects.all()
    serializer_class = EnvironmentSerializer
    permission_classes = [SmartRBACPermission]

    resource_code = 'resource:environments'


class ResourcePoolViewSet(DataScopeMixin, viewsets.ModelViewSet):
    queryset = ResourcePool.objects.all()
    serializer_class = ResourceSerializer
    permission_classes = [SmartRBACPermission]
    filterset_class = ResourcePoolFilter
    resource_type = 'resource_pool'
    resource_code = 'resource:resource_pools'
    asset_share_type = 'resource_pool'

    def perform_create(self, serializer):
        serializer.save(project=getattr(self.request, 'project', None))


class PlatformViewSet(DataScopeMixin, viewsets.ModelViewSet):
    queryset = Platform.objects.all()
    serializer_class = PlatformSerializer
    permission_classes = [SmartRBACPermission]

    resource_code = 'resource:platforms'

    def perform_create(self, serializer):
        serializer.save(project=getattr(self.request, 'project', None))

    @action(detail=True, methods=['post'])
    def sync_assets(self, request, pk=None):
        """
        触发资产同步任务
        """
        platform = self.get_object()
        task = sync_platform_assets.delay(platform.id)
        return Response(
            {"message": "资产同步任务已下发", "task_id": task.id},
            status=status.HTTP_202_ACCEPTED,
        )

    @action(detail=True, methods=['post'])
    def verify_connectivity(self, request, pk=None):
        """
        手动验证指定平台的连通性
        """
        platform = self.get_object()
        task = verify_platform_connectivity.delay(platform.id)
        return Response(
            {"message": "连通性验证任务已下发", "task_id": task.id},
            status=status.HTTP_202_ACCEPTED,
        )


class ComplianceFrameworkViewSet(viewsets.ModelViewSet):
    """
    合规框架管理
    """
    queryset = ComplianceFramework.objects.all()
    serializer_class = ComplianceFrameworkSerializer
    permission_classes = [SmartRBACPermission]
    resource_code = 'resource:compliance'


class ComplianceClauseViewSet(viewsets.ModelViewSet):
    """
    合规条款管理
    """
    queryset = ComplianceClause.objects.all().order_by('sort_order', 'id')
    serializer_class = ComplianceClauseSerializer
    permission_classes = [SmartRBACPermission]
    resource_code = 'resource:compliance'

    @action(detail=True, methods=['post'])
    def trigger_check(self, request, pk=None):
        """
        手动触发该条款关联的所有基线巡检
        """
        clause = self.get_object()
        mappings = clause.baseline_mappings.all()
        if not mappings.exists():
            return Response({"error": "该条款尚未关联任何主机基线"}, status=status.HTTP_400_BAD_REQUEST)
        
        triggered_count = 0
        from apps.host_management.tasks import check_host_baseline
        for m in mappings:
            check_host_baseline.delay(m.baseline.id)
            triggered_count += 1
            
        return Response({"message": f"成功下发 {triggered_count} 个基线巡检任务"})


class ComplianceBaselineMappingViewSet(viewsets.ModelViewSet):
    """
    条款基线映射关系管理
    """
    queryset = ComplianceBaselineMapping.objects.all()
    serializer_class = ComplianceBaselineMappingSerializer
    permission_classes = [SmartRBACPermission]
    resource_code = 'resource:compliance'
