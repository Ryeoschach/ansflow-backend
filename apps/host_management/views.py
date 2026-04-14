from rest_framework import viewsets, status
from rest_framework.response import Response
import paramiko
import io

from apps.host_management.models import Host, Environment, ResourcePool, Platform, SshCredential

from apps.host_management.serializers import HostSerializer, EnvironmentSerializer, PlatformSerializer, \
    ResourceSerializer, SshCredentialSerializer
from utils.rbac_permission import SmartRBACPermission, DataScopeMixin
from rest_framework.decorators import action


class SshCredentialViewSet(DataScopeMixin, viewsets.ModelViewSet):
    queryset = SshCredential.objects.all()
    serializer_class = SshCredentialSerializer
    permission_classes = [SmartRBACPermission]
    resource_type = 'credential'

    resource_code = 'credential:ssh_credentials'

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
from apps.host_management.tasks import verify_platform_connectivity, sync_platform_assets




class HostViewSet(viewsets.ModelViewSet):
    queryset = Host.objects.all()
    serializer_class = HostSerializer
    permission_classes = [SmartRBACPermission]
    filterset_class = HostFilter

    resource_code = 'resource:hosts'

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


class PlatformViewSet(viewsets.ModelViewSet):
    queryset = Platform.objects.all()
    serializer_class = PlatformSerializer
    permission_classes = [SmartRBACPermission]

    resource_code = 'resource:platforms'

    @action(detail=True, methods=['post'])
    def sync_assets(self, request, pk=None):
        """
        触发资产同步任务
        """
        platform = self.get_object()
        result = sync_platform_assets(platform.id)
        return Response({"message": result})

    @action(detail=True, methods=['post'])
    def verify_connectivity(self, request, pk=None):
        """
        手动验证指定平台的连通性
        """
        platform = self.get_object()
        # 立即执行验证任务
        # 测试阶段为同步运行(Todo: 后面改为异步 verify_platform_connectivity.delay(platform.id))。
        verify_platform_connectivity(platform.id)
        
        # 重新获取对象返回最新状态
        platform.refresh_from_db()
        serializer = self.get_serializer(platform)
        return Response(serializer.data)