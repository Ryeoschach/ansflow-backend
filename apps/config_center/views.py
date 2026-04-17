from rest_framework import viewsets, permissions
from rest_framework.decorators import action
from rest_framework.response import Response
from django_filters.rest_framework import DjangoFilterBackend

from .models import ConfigCategory, ConfigItem, ConfigChangeLog
from .serializers import (
    ConfigCategorySerializer,
    ConfigCategorySimpleSerializer,
    ConfigItemSerializer,
    ConfigChangeLogSerializer,
    ConfigRollbackSerializer
)
from utils.rbac_permission import SmartRBACPermission


def get_client_ip(request):
    """获取客户端IP"""
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        return x_forwarded_for.split(',')[0]
    return request.META.get('REMOTE_ADDR')


class ConfigCategoryViewSet(viewsets.ModelViewSet):
    """
    配置分类管理
    """
    queryset = ConfigCategory.objects.all()
    serializer_class = ConfigCategorySerializer
    permission_classes = [SmartRBACPermission]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['name']
    search_fields = ['name', 'label', 'description']
    ordering_fields = ['id', 'name', 'create_time']

    resource_code = 'config:category'

    def get_serializer_class(self):
        if self.action == 'list':
            return ConfigCategorySimpleSerializer
        return ConfigCategorySerializer

    def get_permissions(self):
        if self.action == 'list':
            return [permissions.IsAuthenticated()]
        return super().get_permissions()


class ConfigItemViewSet(viewsets.ModelViewSet):
    """
    配置项管理
    """
    queryset = ConfigItem.objects.filter(is_active=True)
    serializer_class = ConfigItemSerializer
    permission_classes = [SmartRBACPermission]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['category', 'key', 'value_type', 'is_active']
    search_fields = ['key', 'description']
    ordering_fields = ['id', 'key', 'create_time']

    resource_code = 'config:item'

    def get_queryset(self):
        queryset = super().get_queryset()
        category_name = self.request.query_params.get('category_name')
        if category_name:
            queryset = queryset.filter(category__name=category_name)
        return queryset

    def _create_change_log(self, item, action, old_value=None, new_value=None, request=None, reason=''):
        """创建变更日志"""
        operator = request.user if request and hasattr(request, 'user') and request.user.is_authenticated else None
        ConfigChangeLog.objects.create(
            item=item,
            action=action,
            old_value=old_value,
            new_value=new_value,
            operator=operator,
            operator_username=operator.username if operator else 'system',
            ip_address=get_client_ip(request) if request else None,
            reason=reason
        )

    def perform_create(self, serializer):
        instance = serializer.save()
        self._create_change_log(instance, 'create', new_value=instance.value, request=self.request)
        self._notify_config_changed(instance.category.name, instance.key)

    def perform_update(self, serializer):
        old_instance = self.get_object()
        old_value = old_instance.value

        instance = serializer.save()
        self._create_change_log(instance, 'update', old_value=old_value, new_value=instance.value, request=self.request)
        self._notify_config_changed(instance.category.name, instance.key)

    def perform_destroy(self, instance):
        old_value = instance.value
        self._create_change_log(instance, 'delete', old_value=old_value, request=self.request)

        instance.is_active = False
        instance.save()
        self._notify_config_changed(instance.category.name, instance.key)

    def _notify_config_changed(self, category, key, value=None):
        """通知配置变更"""
        # 1. 清除缓存
        from utils.config_manager import ConfigCache
        ConfigCache.invalidate(category, key)

        # 2. 通知订阅者
        from utils.config_manager import ConfigNotifier
        if value is None:
            try:
                item = ConfigItem.objects.get(category__name=category, key=key, is_active=True)
                value = item.get_value()
            except ConfigItem.DoesNotExist:
                pass
        if value is not None:
            ConfigNotifier.notify(category, key, value)

        # 3. 广播到其他实例（多实例同步）
        from utils.config_broadcast import broadcast_config_change
        broadcast_config_change(category, key, value)

        # 4. 发送 Django 信号
        from utils.signals import config_changed
        config_changed.send(sender=self.__class__, category=category, key=key, value=value)

    @action(detail=False, methods=['get'])
    def by_category(self, request):
        """获取指定分类下的所有配置"""
        category_name = request.query_params.get('name')
        if not category_name:
            return Response({'error': '缺少分类名称'}, status=400)

        try:
            category = ConfigCategory.objects.get(name=category_name)
        except ConfigCategory.DoesNotExist:
            return Response({'error': '分类不存在'}, status=404)

        items = ConfigItem.objects.filter(category=category, is_active=True)
        serializer = ConfigItemSerializer(items, many=True)
        return Response({
            'category': category.label,
            'items': serializer.data
        })

    @action(detail=True, methods=['post'])
    def validate_value(self, request, pk=None):
        """验证配置值的合法性"""
        item = self.get_object()
        value = request.data.get('value')

        if value is None:
            return Response({'valid': False, 'error': '缺少 value 字段'}, status=400)

        value_type = item.value_type
        try:
            if value_type == 'int':
                int(value)
            elif value_type == 'float':
                float(value)
            elif value_type == 'bool':
                if not isinstance(value, bool):
                    raise ValueError()
            elif value_type == 'json':
                if not isinstance(value, dict):
                    raise ValueError()
            return Response({'valid': True})
        except (ValueError, TypeError):
            return Response({'valid': False, 'error': f'值必须是 {value_type} 类型'})

    @action(detail=True, methods=['post'])
    def rollback(self, request, pk=None):
        """回滚到指定的历史版本"""
        serializer = ConfigRollbackSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=400)

        item = self.get_object()
        change_log_id = serializer.validated_data['change_log_id']
        reason = serializer.validated_data.get('reason', '')

        try:
            change_log = ConfigChangeLog.objects.get(id=change_log_id, item=item)
        except ConfigChangeLog.DoesNotExist:
            return Response({'error': '变更记录不存在'}, status=404)

        # 不能回滚到 null（创建操作没有旧值）
        if change_log.old_value is None:
            return Response({'error': '无法回滚：创建操作没有旧值可回滚'}, status=400)

        # 执行回滚
        old_value = item.value
        item.value = change_log.old_value
        item.save()

        # 记录回滚日志
        self._create_change_log(item, 'rollback', old_value=old_value, new_value=item.value, request=request, reason=reason)

        # 通知变更
        self._notify_config_changed(item.category.name, item.key)

        return Response({
            'message': '回滚成功',
            'old_value': old_value,
            'new_value': item.value
        })


class ConfigChangeLogViewSet(viewsets.ReadOnlyModelViewSet):
    """
    配置变更日志（只读）
    """
    queryset = ConfigChangeLog.objects.all()
    serializer_class = ConfigChangeLogSerializer
    permission_classes = [SmartRBACPermission]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['item', 'action', 'operator_username']
    search_fields = ['item__key', 'operator_username', 'reason']
    ordering_fields = ['id', 'create_time']

    resource_code = 'config:change_log'

    def get_queryset(self):
        queryset = super().get_queryset()
        item_id = self.request.query_params.get('item_id')
        if item_id:
            queryset = queryset.filter(item_id=item_id)
        return queryset
