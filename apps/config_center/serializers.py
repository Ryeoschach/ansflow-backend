from rest_framework import serializers
from .models import ConfigCategory, ConfigItem


class ConfigItemSerializer(serializers.ModelSerializer):
    """配置项序列化器"""
    value_display = serializers.SerializerMethodField()

    class Meta:
        model = ConfigItem
        fields = [
            'id', 'category', 'key', 'value', 'value_type',
            'is_encrypted', 'is_active', 'description',
            'create_time', 'update_time', 'value_display'
        ]
        read_only_fields = ['id', 'create_time', 'update_time']

    def get_value_display(self, obj):
        """返回值的显示，加密字段返回掩码"""
        if obj.is_encrypted:
            return '******'
        return obj.value

    def validate(self, attrs):
        """验证值类型"""
        value = attrs.get('value')
        value_type = attrs.get('value_type', 'string')

        if value is not None:
            if value_type == 'int' and not isinstance(value, int):
                try:
                    int(value)
                except (ValueError, TypeError):
                    raise serializers.ValidationError({'value': '值必须是整数'})
            elif value_type == 'float' and not isinstance(value, (int, float)):
                try:
                    float(value)
                except (ValueError, TypeError):
                    raise serializers.ValidationError({'value': '值必须是浮点数'})
            elif value_type == 'bool' and not isinstance(value, bool):
                raise serializers.ValidationError({'value': '值必须是布尔值'})
            elif value_type == 'json' and not isinstance(value, dict):
                raise serializers.ValidationError({'value': '值必须是 JSON 对象'})

        return attrs


class ConfigCategorySerializer(serializers.ModelSerializer):
    """配置分类序列化器"""
    items = ConfigItemSerializer(many=True, read_only=True)
    item_count = serializers.SerializerMethodField()

    class Meta:
        model = ConfigCategory
        fields = ['id', 'name', 'label', 'description', 'item_count', 'items', 'create_time', 'update_time']
        read_only_fields = ['id', 'create_time', 'update_time']

    def get_item_count(self, obj):
        return obj.items.filter(is_active=True).count()


class ConfigCategorySimpleSerializer(serializers.ModelSerializer):
    """配置分类简洁序列化器"""
    item_count = serializers.SerializerMethodField()

    class Meta:
        model = ConfigCategory
        fields = ['id', 'name', 'label', 'description', 'item_count', 'create_time', 'update_time']
        read_only_fields = ['id', 'create_time', 'update_time']

    def get_item_count(self, obj):
        return obj.items.filter(is_active=True).count()
