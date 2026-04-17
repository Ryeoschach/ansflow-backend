from rest_framework import viewsets, permissions
from rest_framework.decorators import action
from rest_framework.response import Response
from django_filters.rest_framework import DjangoFilterBackend

from .models import ConfigCategory, ConfigItem
from .serializers import (
    ConfigCategorySerializer,
    ConfigCategorySimpleSerializer,
    ConfigItemSerializer
)
from utils.rbac_permission import SmartRBACPermission


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
        # 支持通过 category__name 过滤
        category_name = self.request.query_params.get('category_name')
        if category_name:
            queryset = queryset.filter(category__name=category_name)
        return queryset

    def perform_create(self, serializer):
        instance = serializer.save()
        self._notify_config_changed(instance.category.name, instance.key)

    def perform_update(self, serializer):
        instance = serializer.save()
        self._notify_config_changed(instance.category.name, instance.key)

    def perform_destroy(self, instance):
        # 软删除
        instance.is_active = False
        instance.save()
        self._notify_config_changed(instance.category.name, instance.key)

    def _notify_config_changed(self, category, key):
        """通知配置变更"""
        # 1. 清除缓存
        from utils.config_manager import ConfigCache
        ConfigCache.invalidate(category, key)

        # 2. 通知订阅者
        from utils.config_manager import ConfigNotifier
        from apps.config_center.models import ConfigItem
        try:
            item = ConfigItem.objects.get(category__name=category, key=key, is_active=True)
            value = item.get_value()
        except ConfigItem.DoesNotExist:
            value = None
        ConfigNotifier.notify(category, key, value)

        # 3. 发送 Django 信号（用于外部系统监听）
        from utils.signals import config_changed
        config_changed.send(
            sender=self.__class__,
            category=category,
            key=key,
            value=value
        )

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

        # 类型验证
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
