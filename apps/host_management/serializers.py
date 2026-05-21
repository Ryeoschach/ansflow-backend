from rest_framework import serializers
from apps.host_management.models import Host, Environment, Platform, ResourcePool, SshCredential, HostBaseline


class SshCredentialSerializer(serializers.ModelSerializer):
    class Meta:
        model = SshCredential
        fields = '__all__'
        extra_kwargs = {
            'password': {'write_only': True},
            'private_key': {'write_only': True},
            'passphrase': {'write_only': True},
        }


class HostSerializer(serializers.ModelSerializer):

    def validate_private_ip(self, value):
        """
        验证内网 IP 是否符合 RFC1918 私有地址规范
        私有地址校验：利用 ip.is_private 属性，它可以自动识别 RFC1918 定义的私有网段：
            10.0.0.0 - 10.255.255.255
            172.16.0.0 - 172.31.255.255
            192.168.0.0 - 192.168.255.255
        """
        import ipaddress
        if not value:
            return value
            
        try:
            ip = ipaddress.ip_address(value)
            if not ip.is_private:
                raise serializers.ValidationError("提供的 IP 不是内网私有地址（需符合 RFC1918 规范）。")
        except ValueError:
            raise serializers.ValidationError("无效的 IP 地址格式。")
            
        return value

    platform_name = serializers.CharField(source='platform.name', read_only=True)
    env_name = serializers.CharField(source='env.name', read_only=True)
    env_color = serializers.CharField(source='env.color', read_only=True)
    credential_name = serializers.CharField(source='credential.name', read_only=True)

    class Meta:
        model = Host
        fields = ['id', 'hostname', 'ports', 'ip_address', 'private_ip', 'os_type', 'cpu', 'memory', 'disk', 'status', 'env', 'platform', 'platform_name', 'env_name', 'env_color', 'credential', 'credential_name', 'create_time', 'update_time']


class EnvironmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Environment
        fields = '__all__'

class PlatformSerializer(serializers.ModelSerializer):
    default_credential_name = serializers.CharField(source='default_credential.name', read_only=True)

    class Meta:
        model = Platform
        fields = ['id', 'name', 'type', 'access_key', 'secret_key', 'api_endpoint', 
                  'connectivity_status', 'last_verified_at', 'error_message', 'status', 
                  'default_credential', 'default_credential_name', 'create_time', 'update_time']
        read_only_fields = ['connectivity_status', 'last_verified_at', 'error_message']
        extra_kwargs = {
            'access_key': {'write_only': True},
            'secret_key': {'write_only': True}  # 敏感信息不返回前端
        }


class ResourceSerializer(serializers.ModelSerializer):
    host_details = HostSerializer(source='hosts', many=True, read_only=True)

    class Meta:
        model = ResourcePool
        fields = ['id', 'name', 'code', 'hosts', 'remark', 'create_time', 'update_time', 'host_details']


class HostBaselineSerializer(serializers.ModelSerializer):
    """
    主机基线序列化器
    """
    pool_name = serializers.ReadOnlyField(source='resource_pool.name')

    class Meta:
        model = HostBaseline
        fields = [
            'id', 'name', 'resource_pool', 'pool_name', 
            'check_playbook', 'auto_remediate', 'remediate_playbook',
            'is_active', 'last_check_time', 'last_check_status', 'last_execution_id',
            'create_time', 'update_time'
        ]
        read_only_fields = ['last_check_time', 'last_check_status', 'last_execution_id', 'create_time', 'update_time']