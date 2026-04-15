# AnsFlow 后端 - 项目指令

> 本文件为 AnsFlow 后端项目提供持久上下文，覆盖所有在此目录中工作的开发者。

---

## 项目概述

AnsFlow 是一个企业级 DevOps 平台，后端采用 Django 5.2 + Django REST Framework 构建。

### 技术栈

| 类别 | 技术 |
|------|------|
| 框架 | Django 5.2 + DRF 3.16 |
| 语言 | Python 3.12+ |
| ORM | Django ORM (SQLite dev / PostgreSQL prod) |
| 认证 | JWT via SimpleJWT |
| 异步任务 | Celery 5.x + Redis |
| 实时通信 | Django Channels + WebSocket |
| 缓存 | Redis + django-redis |
| API 文档 | drf-spectacular (Swagger/OpenAPI) |

### 目录结构

```
backend/
├── apps/                      # 业务应用模块
│   ├── rbac_permission/       # 用户/角色/权限/菜单/审计
│   ├── host_management/       # 主机管理
│   ├── task_management/       # Ansible 任务
│   ├── pipeline_management/   # 流水线 (DAG + 调度)
│   ├── k8s_management/        # K8s 多集群 + Helm
│   ├── registry_management/   # Docker 镜像仓库
│   ├── approval_center/       # 审批工作流
│   ├── credentials_management/# 凭证存储
│   └── system_management/     # 系统设置
├── config/                    # Django 项目配置
│   ├── settings/              # settings 分层 (base / development / production)
│   ├── asgi.py               # ASGI 配置 (WebSocket 支持)
│   ├── celery.py             # Celery 配置
│   ├── routing.py            # Channels 路由
│   └── urls.py                # 全局 URL 路由
└── utils/                    # 共享工具
```

### API 路由

所有 API 前缀: `/api/v1/`

| 模块 | 路由前缀 |
|------|---------|
| 认证 | `/api/v1/auth/` |
| 用户/角色/权限 | `/api/v1/rbac/` |
| 主机管理 | `/api/v1/hosts/` |
| 任务中心 | `/api/v1/tasks/` |
| 流水线 | `/api/v1/pipelines/` |
| K8s 管理 | `/api/v1/k8s/` |
| Helm 应用 | `/api/v1/helm/` |
| 镜像仓库 | `/api/v1/registries/` |
| 审批中心 | `/api/v1/approvals/` |
| 凭证管理 | `/api/v1/credentials/` |
| 系统 | `/api/v1/system/` |

---

## 开发规范

### 命名约定

- **Model**: PascalCase (如 `PipelineRun`)
- **ViewSet**: PascalCase 以 `ViewSet` 结尾 (如 `PipelineViewSet`)
- **Serializer**: PascalCase 以 `Serializer` 结尾
- **Task**: snake_case (如 `advance_pipeline_engine`)
- **权限码**: `snake_case` 格式 (如 `pipeline:template:view`)

### App 组织结构

每个业务 app 包含:

```
app/
├── __init__.py
├── admin.py
├── apps.py
├── models.py
├── views.py
├── serializers.py
├── urls.py
├── filters.py
├── tasks.py        # Celery tasks
└── migrations/
```

### Model 规范

- 使用 `utils/base_model.py` 中的 `BaseModel` 作为基类
- 包含 `id`, `create_time`, `update_time` 字段

```python
from utils.base_model import BaseModel

class Pipeline(BaseModel):
    name = models.CharField(max_length=255)
    # ...
```

### ViewSet 规范

```python
class PipelineViewSet(DataScopeMixin, viewsets.ModelViewSet):
    queryset = Pipeline.objects.all()
    serializer_class = PipelineSerializer
    permission_classes = [SmartRBACPermission]
    resource_code = 'pipeline:template'
    resource_type = 'pipeline'
    resource_owner_field = 'creator'
    filterset_fields = ['pipeline', 'status']
    search_fields = ['pipeline__name', 'trigger_user__username']
```

### 权限系统 (SmartRBAC)

权限码格式: `{resource}:{action}`

```python
RBAC_ACTION_MAP = {
    'list': 'view', 'get': 'view', 'retrieve': 'view',
    'create': 'add', 'post': 'add',
    'update': 'edit', 'put': 'edit', 'partial_update': 'edit', 'patch': 'edit',
    'destroy': 'delete', 'delete': 'delete',
}
```

### 认证

- JWT Token: Access token 60 分钟，Refresh token 7 天
- Token 存储在 Cookie 中
- 自定义视图: `CookieTokenObtainPairView`, `CookieTokenRefreshView`

### Celery 任务

- Broker: Redis
- Backend: Django DB
- 调度器: `django_celery_beat` with `DatabaseScheduler`

### 配置管理

- 使用 `django-environ` 管理环境变量
- Settings 分层: `base.py` -> `development.py` / `production.py`
- 敏感配置从 `.env` 文件加载

---

## 常用命令

| 命令 | 说明 |
|------|------|
| `python manage.py runserver` | 开发服务器 |
| `python manage.py makemigrations` | 生成迁移 |
| `python manage.py migrate` | 执行迁移 |
| `celery -A config worker -l INFO` | 启动 Celery worker |
| `celery -A config beat -l INFO` | 启动 Celery beat |
| `python manage.py spectacular --file schema.yml` | 生成 OpenAPI Schema |

---

## 相关文档

- 详细规范见 `.claude/rules/` 目录
- 项目 README: `@README.md`
