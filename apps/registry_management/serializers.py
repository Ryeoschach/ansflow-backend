from rest_framework import serializers
from .models import ImageRegistry

class ImageRegistrySerializer(serializers.ModelSerializer):
    class Meta:
        model = ImageRegistry
        fields = '__all__'
        extra_kwargs = {
            'password': {'write_only': True} # 密码只写，不返回给前端
        }
