from rest_framework import serializers
from apps.host_management.models import (
    Host, Environment, Platform, ResourcePool, SshCredential, HostBaseline,
    ComplianceFramework, ComplianceClause, ComplianceBaselineMapping
)


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

    def validate(self, attrs):
        project = getattr(self.context.get('request'), 'project', None)
        if not project:
            return attrs

        platform = attrs.get('platform')
        credential = attrs.get('credential')
        if platform and platform.project_id not in (None, project.id):
            raise serializers.ValidationError({
                'platform': '所选平台不属于当前项目。'
            })
        if credential and credential.project_id not in (None, project.id):
            raise serializers.ValidationError({
                'credential': '所选凭据不属于当前项目。'
            })
        return attrs

    class Meta:
        model = Host
        fields = ['id', 'hostname', 'ports', 'ip_address', 'private_ip', 'os_type', 'cpu', 'memory', 'disk', 'status', 'env', 'platform', 'platform_name', 'env_name', 'env_color', 'credential', 'credential_name', 'project', 'create_time', 'update_time']
        read_only_fields = ['project']


class EnvironmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Environment
        fields = '__all__'

class PlatformSerializer(serializers.ModelSerializer):
    default_credential_name = serializers.CharField(source='default_credential.name', read_only=True)

    def validate_default_credential(self, credential):
        project = getattr(self.context.get('request'), 'project', None)
        if project and credential and credential.project_id not in (None, project.id):
            raise serializers.ValidationError('所选默认凭据不属于当前项目。')
        return credential

    class Meta:
        model = Platform
        fields = ['id', 'name', 'type', 'access_key', 'secret_key', 'api_endpoint', 
                  'connectivity_status', 'last_verified_at', 'error_message', 'status', 
                  'default_credential', 'default_credential_name', 'project', 'create_time', 'update_time']
        read_only_fields = ['connectivity_status', 'last_verified_at', 'error_message', 'project']
        extra_kwargs = {
            'access_key': {'write_only': True},
            'secret_key': {'write_only': True}  # 敏感信息不返回前端
        }


class ResourceSerializer(serializers.ModelSerializer):
    host_details = HostSerializer(source='hosts', many=True, read_only=True)

    def validate_hosts(self, hosts):
        project = getattr(self.context.get('request'), 'project', None)
        if project and any(host.project_id != project.id for host in hosts):
            raise serializers.ValidationError('资源池只能包含当前项目的主机。')
        return hosts

    class Meta:
        model = ResourcePool
        fields = ['id', 'name', 'code', 'hosts', 'remark', 'project', 'create_time', 'update_time', 'host_details']
        read_only_fields = ['project']


class HostBaselineSerializer(serializers.ModelSerializer):
    """
    主机基线序列化器
    """
    pool_name = serializers.ReadOnlyField(source='resource_pool.name')

    def validate_resource_pool(self, resource_pool):
        project = getattr(self.context.get('request'), 'project', None)
        if project and resource_pool.project_id != project.id:
            raise serializers.ValidationError('所选资源池不属于当前项目。')
        return resource_pool

    class Meta:
        model = HostBaseline
        fields = [
            'id', 'name', 'resource_pool', 'pool_name', 
            'check_playbook', 'auto_remediate', 'remediate_playbook',
            'is_active', 'last_check_time', 'last_check_status', 'last_execution_id',
            'create_time', 'update_time'
        ]
        read_only_fields = ['last_check_time', 'last_check_status', 'last_execution_id', 'create_time', 'update_time']


class ComplianceFrameworkSerializer(serializers.ModelSerializer):
    clause_count = serializers.IntegerField(source='clauses.count', read_only=True)
    
    class Meta:
        model = ComplianceFramework
        fields = ['id', 'name', 'code', 'version', 'description', 'clause_count', 'create_time', 'update_time']
        read_only_fields = ['create_time', 'update_time']


class ComplianceClauseSerializer(serializers.ModelSerializer):
    compliance_status = serializers.ReadOnlyField()
    baseline_count = serializers.IntegerField(source='baseline_mappings.count', read_only=True)
    baseline_details = serializers.SerializerMethodField()

    class Meta:
        model = ComplianceClause
        fields = [
            'id', 'framework', 'parent', 'code', 'name', 'description', 
            'sort_order', 'compliance_status', 'baseline_count', 'baseline_details',
            'create_time', 'update_time'
        ]
        read_only_fields = ['create_time', 'update_time']

    def get_baseline_details(self, obj):
        mappings = obj.baseline_mappings.select_related('baseline', 'baseline__resource_pool')
        return [
            {
                "mapping_id": m.id,
                "baseline_id": m.baseline.id,
                "baseline_name": m.baseline.name,
                "pool_name": m.baseline.resource_pool.name if m.baseline.resource_pool else None,
                "last_check_status": m.baseline.last_check_status,
                "last_check_time": m.baseline.last_check_time.isoformat() if m.baseline.last_check_time else None
            } for m in mappings
        ]


class ComplianceBaselineMappingSerializer(serializers.ModelSerializer):
    baseline_name = serializers.ReadOnlyField(source='baseline.name')
    pool_name = serializers.ReadOnlyField(source='baseline.resource_pool.name')
    clause_code = serializers.ReadOnlyField(source='clause.code')
    clause_name = serializers.ReadOnlyField(source='clause.name')

    class Meta:
        model = ComplianceBaselineMapping
        fields = [
            'id', 'clause', 'clause_code', 'clause_name', 
            'baseline', 'baseline_name', 'pool_name', 
            'create_time', 'update_time'
        ]
        read_only_fields = ['create_time', 'update_time']
