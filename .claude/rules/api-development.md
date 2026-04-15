---
paths:
  - "apps/**/serializers.py"
  - "apps/**/filters.py"
  - "config/api_router.py"
---

# API 开发规则

## URL 路由

所有 API 统一在 `config/api_router.py` 注册:

```python
from rest_framework.routers import DefaultRouter

router = DefaultRouter()
router.register(r'pipelines', PipelineViewSet, basename='pipeline')
router.register(r'tasks', TaskViewSet, basename='task')
# ...

urlpatterns = [
    path('api/v1/', router.urls),
]
```

## Serializer 规范

### 基础规范

```python
class PipelineSerializer(serializers.ModelSerializer):
    class Meta:
        model = Pipeline
        fields = ['id', 'name', 'description', 'status', 'create_time', 'update_time']
        read_only_fields = ['id', 'create_time', 'update_time']
```

### 嵌套 Serializer

```python
class NodeSerializer(serializers.ModelSerializer):
    class Meta:
        model = PipelineNode
        fields = ['id', 'name', 'type', 'config']

class PipelineDetailSerializer(PipelineSerializer):
    nodes = NodeSerializer(many=True, read_only=True)

    class Meta(PipelineSerializer.Meta):
        fields = PipelineSerializer.Meta.fields + ['nodes']
```

### Write-Only 敏感字段

```python
password = serializers.CharField(write_only=True)
```

### 动态字段

```python
class PipelineSerializer(serializers.ModelSerializer):
    detail = serializers.SerializerMethodField()

    def get_detail(self, obj):
        if self.context.get('include_detail'):
            return PipelineDetailSerializer(obj).data
        return None
```

## Filter 规范

```python
from django_filters import rest_framework as filters

class PipelineFilter(filters.FilterSet):
    status = filters.ChoiceFilter(choices=Pipeline.Status.choices)
    created_after = filters.DateTimeFilter(field_name='create_time', lookup_expr='gte')
    name = filters.CharFilter(lookup_expr='icontains')

    class Meta:
        model = Pipeline
        fields = ['status', 'name', 'created_after']
```

## Pagination

使用全局分页器配置，统一使用 `utils/pagination.py` 中的自定义分页器。

## API 文档

使用 `drf-spectacular` 生成 OpenAPI 文档:

```python
from drf_spectacular.utils import extend_schema, extend_schema_view

@extend_schema_view(
    list=extend_schema(description='列出所有流水线'),
    retrieve=extend_schema(description='获取流水线详情'),
    create=extend_schema(description='创建流水线'),
)
class PipelineViewSet(viewsets.ModelViewSet):
    ...
```

## 错误处理

- 使用 DRF 内置异常处理
- 全局异常处理器在 `utils/exception_handler.py`
- 自定义异常需要继承 `APIException`

## 不要做的事

- **不要**在 Serializer 中处理业务逻辑
- **不要**返回字典，使用 Serializer
- **不要**忽略 Filter 的 SQL 注入风险，使用 django-filter
