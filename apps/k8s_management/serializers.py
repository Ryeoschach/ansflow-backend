from rest_framework import serializers
from .models import K8sCluster


class K8sClusterSerializer(serializers.ModelSerializer):
    """
    K8s 集群序列化器
    """

    class Meta:
        model = K8sCluster
        # 定义暴露的所有字段
        fields = [
            'id', 'name', 'auth_type', 'kubeconfig_content',
            'api_server', 'token', 'status', 'version',
            'remark', 'create_time', 'update_time'
        ]

        # 这些字段由系统自动生成或通过 /verify/ 接口更新，前端不可手动修改
        read_only_fields = ['status', 'version', 'create_time', 'update_time']

        # 安全脱敏：敏感信息只允许写入，禁止读出
        extra_kwargs = {
            'kubeconfig_content': {'write_only': True, 'required': False},
            'token': {'write_only': True, 'required': False},
            'api_server': {'required': False},
        }

    def validate(self, data):
        """
        逻辑校验：根据 auth_type 确保必填项完整
        """
        auth_type = data.get('auth_type')

        if auth_type == 'kubeconfig':
            if not data.get('kubeconfig_content'):
                raise serializers.ValidationError({"kubeconfig_content": "Kubeconfig 模式必须上传配置文件内容"})

        elif auth_type == 'token':
            if not data.get('api_server'):
                raise serializers.ValidationError({"api_server": "Token 模式必须输入 API Server 地址"})
            if not data.get('token'):
                raise serializers.ValidationError({"token": "Token 模式必须输入认证 Token"})

        return data
