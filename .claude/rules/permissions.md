---
paths:
  - "apps/**/views.py"
  - "utils/rbac_permission.py"
---

# 权限系统规则

## SmartRBAC 概述

权限码格式: `{resource}:{action}`

示例:
- `pipeline:template:view` - 查看流水线
- `pipeline:template:add` - 创建流水线
- `pipeline:template:edit` - 编辑流水线
- `pipeline:template:delete` - 删除流水线
- `pipeline:template:execute` - 执行流水线

## Action 映射

```python
RBAC_ACTION_MAP = {
    'list': 'view', 'get': 'view', 'retrieve': 'view',
    'create': 'add', 'post': 'add',
    'update': 'edit', 'put': 'edit', 'partial_update': 'edit', 'patch': 'edit',
    'destroy': 'delete', 'delete': 'delete',
}
```

## ViewSet 权限配置

```python
class PipelineViewSet(DataScopeMixin, viewsets.ModelViewSet):
    resource_code = 'pipeline:template'  # 资源码前缀
    permission_classes = [SmartRBACPermission]
```

### 权限检查流程

1. DRF 默认 action 映射到权限 action
2. `SmartRBACPermission` 根据 `resource_code` + action 构建权限码
3. 查询用户角色关联的权限
4. 检查用户是否有该权限码

## 自定义 Action 权限

自定义 action 方法名自动映射为权限码后缀:

```python
@action(detail=True, methods=['post'])
def execute(self, request, pk=None):
    """执行流水线 — 需要 pipeline:template:execute 权限"""
    pass

@action(detail=True, methods=['post'])
def force_stop(self, request, pk=None):
    """强制停止 — 需要 pipeline:template:force_stop 权限"""
    pass
```

## DataScopeMixin (数据权限)

用于过滤数据范围:

```python
class PipelineViewSet(DataScopeMixin, viewsets.ModelViewSet):
    resource_type = 'pipeline'           # 数据策略的资源类型
    resource_owner_field = 'creator'     # 所有者字段
```

用户只能看到:
- 自己创建的数据
- 角色数据策略允许访问的数据

## Admin 权限

Admin 不经过 SmartRBAC，直接使用 Django admin 权限。

## 不要做的事

- **不要**在 ViewSet 中手动检查权限，使用 SmartRBACPermission
- **不要**在代码中硬编码权限码字符串，使用常量或枚举
- **不要**绕过 SmartRBAC 进行数据过滤
