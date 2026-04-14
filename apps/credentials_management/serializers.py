from rest_framework import serializers
from .models import Credential

class CredentialSerializer(serializers.ModelSerializer):
    """
    自动对敏感内容执行打码屏蔽
    """
    class Meta:
        model = Credential
        fields = ['id', 'name', 'type', 'username', 'secret_value', 'description', 'create_time', 'update_time']
        
    def to_representation(self, instance):
        """
        获取数据时，如果存在敏感值，只返回打码后的标记内容，
        防止敏感数据在前端控制台和 API 传输中明文暴露。
        真实密文只在具体的执行节点（宿主机）解密加载。
        """
        data = super().to_representation(instance)
        if data.get('secret_value'):
            data['secret_value'] = '******** (Secret Encrypted by Vault)'
        return data

    def validate(self, data):
        """
        如果用户在回传时没有传 secret_value，说明不想修改现有的加密串。
        """
        return data
