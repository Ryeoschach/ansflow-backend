from rest_framework import serializers
from .models import ConfigCategory, ConfigItem, ConfigChangeLog
from .registry import get_category_definition, get_item_definition, is_system_category


class ConfigItemSerializer(serializers.ModelSerializer):
    """配置项序列化器"""
    value_display = serializers.SerializerMethodField()
    category_name = serializers.CharField(source='category.name', read_only=True)
    is_system = serializers.SerializerMethodField()
    registered = serializers.SerializerMethodField()
    default_value = serializers.SerializerMethodField()
    module = serializers.SerializerMethodField()
    readonly_key = serializers.SerializerMethodField()
    allow_delete = serializers.SerializerMethodField()
    config_scope = serializers.SerializerMethodField()

    class Meta:
        model = ConfigItem
        fields = [
            'id', 'category', 'category_name', 'key', 'value', 'value_type',
            'is_encrypted', 'is_active', 'description',
            'create_time', 'update_time', 'value_display',
            'is_system', 'registered', 'default_value', 'module',
            'readonly_key', 'allow_delete', 'config_scope'
        ]
        read_only_fields = ['id', 'create_time', 'update_time']

    def get_value_display(self, obj) -> str:
        """返回值的显示，加密字段返回掩码"""
        if obj.is_encrypted:
            return '******'
        return obj.value

    def _definition(self, obj):
        return get_item_definition(obj.category.name, obj.key)

    def get_is_system(self, obj) -> bool:
        return self._definition(obj) is not None

    def get_registered(self, obj) -> bool:
        return self._definition(obj) is not None

    def get_default_value(self, obj):
        definition = self._definition(obj)
        return definition.default_value if definition else None

    def get_module(self, obj) -> str:
        definition = self._definition(obj)
        return definition.module if definition else 'custom'

    def get_readonly_key(self, obj) -> bool:
        definition = self._definition(obj)
        return bool(definition and not definition.allow_key_edit)

    def get_allow_delete(self, obj) -> bool:
        definition = self._definition(obj)
        return bool(definition.allow_delete) if definition else True

    def get_config_scope(self, obj) -> str:
        return 'system' if self._definition(obj) else 'custom'

    def validate(self, attrs):
        """验证值类型"""
        value = attrs.get('value')
        instance = getattr(self, 'instance', None)
        value_type = attrs.get('value_type') or (instance.value_type if instance else 'string')
        category = attrs.get('category') or (instance.category if instance else None)
        key = attrs.get('key') or (instance.key if instance else None)
        definition = get_item_definition(category.name, key) if category and key else None

        if definition:
            immutable_fields = {'category', 'key', 'value_type', 'is_encrypted'}
            changed_immutable = immutable_fields.intersection(attrs.keys())
            if instance and changed_immutable:
                raise serializers.ValidationError({
                    field: '系统配置项不允许修改该字段'
                    for field in changed_immutable
                })
            value_type = definition.value_type
        elif category and is_system_category(category.name):
            raise serializers.ValidationError({
                'key': '该分类为系统注册分类，未注册的配置键不会被后端业务识别，请在自定义分类中创建自定义变量'
            })

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
            elif value_type == 'json' and not isinstance(value, (dict, list)):
                raise serializers.ValidationError({'value': '值必须是 JSON 对象或数组'})

        return attrs


class ConfigCategorySerializer(serializers.ModelSerializer):
    """配置分类序列化器"""
    items = ConfigItemSerializer(many=True, read_only=True)
    item_count = serializers.SerializerMethodField()
    is_system = serializers.SerializerMethodField()
    module = serializers.SerializerMethodField()
    allow_custom_items = serializers.SerializerMethodField()
    allow_delete = serializers.SerializerMethodField()

    class Meta:
        model = ConfigCategory
        fields = [
            'id', 'name', 'label', 'description', 'item_count', 'items',
            'create_time', 'update_time', 'is_system', 'module',
            'allow_custom_items', 'allow_delete'
        ]
        read_only_fields = ['id', 'create_time', 'update_time']

    def get_item_count(self, obj) -> int:
        return obj.items.filter(is_active=True).count()

    def _definition(self, obj):
        return get_category_definition(obj.name)

    def get_is_system(self, obj) -> bool:
        return self._definition(obj) is not None

    def get_module(self, obj) -> str:
        definition = self._definition(obj)
        return definition.module if definition else 'custom'

    def get_allow_custom_items(self, obj) -> bool:
        definition = self._definition(obj)
        return definition.allow_custom_items if definition else True

    def get_allow_delete(self, obj) -> bool:
        definition = self._definition(obj)
        return definition.allow_delete if definition else True


class ConfigCategorySimpleSerializer(serializers.ModelSerializer):
    """配置分类简洁序列化器"""
    item_count = serializers.SerializerMethodField()
    is_system = serializers.SerializerMethodField()
    module = serializers.SerializerMethodField()
    allow_custom_items = serializers.SerializerMethodField()
    allow_delete = serializers.SerializerMethodField()

    class Meta:
        model = ConfigCategory
        fields = [
            'id', 'name', 'label', 'description', 'item_count',
            'create_time', 'update_time', 'is_system', 'module',
            'allow_custom_items', 'allow_delete'
        ]
        read_only_fields = ['id', 'create_time', 'update_time']

    def get_item_count(self, obj) -> int:
        return obj.items.filter(is_active=True).count()

    def _definition(self, obj):
        return get_category_definition(obj.name)

    def get_is_system(self, obj) -> bool:
        return self._definition(obj) is not None

    def get_module(self, obj) -> str:
        definition = self._definition(obj)
        return definition.module if definition else 'custom'

    def get_allow_custom_items(self, obj) -> bool:
        definition = self._definition(obj)
        return definition.allow_custom_items if definition else True

    def get_allow_delete(self, obj) -> bool:
        definition = self._definition(obj)
        return definition.allow_delete if definition else True


class ConfigChangeLogSerializer(serializers.ModelSerializer):
    """配置变更日志序列化器"""
    item_key = serializers.CharField(source='item.key', read_only=True)
    item_category = serializers.CharField(source='item.category.name', read_only=True)
    old_value_display = serializers.SerializerMethodField()
    new_value_display = serializers.SerializerMethodField()

    class Meta:
        model = ConfigChangeLog
        fields = [
            'id', 'item', 'item_key', 'item_category',
            'action', 'old_value', 'new_value',
            'old_value_display', 'new_value_display',
            'operator', 'operator_username', 'ip_address',
            'reason', 'create_time'
        ]
        read_only_fields = ['id', 'create_time']

    def get_old_value_display(self, obj) -> str:
        if obj.old_value and isinstance(obj.old_value, str) and obj.old_value.startswith('enc:'):
            return '******'
        return obj.old_value

    def get_new_value_display(self, obj) -> str:
        if obj.new_value and isinstance(obj.new_value, str) and obj.new_value.startswith('enc:'):
            return '******'
        return obj.new_value


class ConfigRollbackSerializer(serializers.Serializer):
    """配置回滚序列化器"""
    change_log_id = serializers.IntegerField(required=True, help_text="变更日志ID")
    reason = serializers.CharField(max_length=500, required=False, default='', help_text="回滚原因")
