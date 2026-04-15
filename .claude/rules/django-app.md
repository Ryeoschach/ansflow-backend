---
paths:
  - "apps/**/models.py"
  - "apps/**/views.py"
  - "apps/**/serializers.py"
---

# Django App 开发规则

## Model 规范

### 使用 BaseModel

所有 Model 继承 `utils/base_model.py` 的 `BaseModel`:

```python
from utils.base_model import BaseModel

class Pipeline(BaseModel):
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    creator = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    # ...
```

### 字段规范

- 外键使用 `ForeignKey` with `on_delete` 策略
- 布尔字段使用 `default=False` 而非 `null`
- 文本字段: `CharField` 用于短文本，`TextField` 用于长文本
- 时间戳使用 `auto_now_add` / `auto_now`

### Meta 类

```python
class Meta:
    verbose_name = '流水线'
    verbose_name_plural = '流水线'
    ordering = ['-create_time']
    indexes = [
        models.Index(fields=['name']),
    ]
```

## ViewSet 规范

### 标准 ViewSet

```python
class PipelineViewSet(DataScopeMixin, viewsets.ModelViewSet):
    queryset = Pipeline.objects.all()
    serializer_class = PipelineSerializer
    permission_classes = [SmartRBACPermission]
    resource_code = 'pipeline:template'
    resource_type = 'pipeline'
    resource_owner_field = 'creator'
    filterset_fields = ['status', 'creator']
    search_fields = ['name', 'description']
    ordering_fields = ['create_time', 'update_time']
```

### 必须声明的字段

- `resource_code`: 权限码前缀 (如 `pipeline:template`)
- `resource_type`: 数据策略过滤的资源类型
- `resource_owner_field`: 所有者字段，用于数据权限豁免

### 自定义 Action

```python
@action(detail=True, methods=['post'])
def execute(self, request, pk=None):
    """执行流水线"""
    # 自动根据 resource_code + 'execute' 检查权限
    pass
```

## Serializer 规范

### 标准 Serializer

```python
class PipelineSerializer(serializers.ModelSerializer):
    creator_name = serializers.CharField(source='creator.username', read_only=True)

    class Meta:
        model = Pipeline
        fields = ['id', 'name', 'description', 'creator', 'creator_name', 'create_time']
        read_only_fields = ['id', 'create_time', 'creator']

    def create(self, validated_data):
        validated_data['creator'] = self.context['request'].user
        return super().create(validated_data)
```

### 嵌套序列化

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

## Admin 规范

```python
@admin.register(Pipeline)
class PipelineAdmin(BaseModelAdmin):
    list_display = ['id', 'name', 'creator', 'status', 'create_time']
    list_filter = ['status', 'create_time']
    search_fields = ['name', 'description']
    readonly_fields = ['create_time', 'update_time']
```

## 不要做的事

- **不要**在 Model 中直接定义业务逻辑，交给 Service 层或 ViewSet
- **不要**使用 `*args, **kwargs` 传递参数，使用明确的字段
- **不要**在 ViewSet 中处理原生 SQL，优先使用 ORM
