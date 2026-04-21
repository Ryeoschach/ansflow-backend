# AnsFlow Backend

企业级 DevOps 流水线平台后端，基于 Django 5.2 + DRF 构建。
demo: https://ansflow.cyfee.com:10443
admin/ansflow

**当前版本**: v1.2.1 (build: 2026-04-21)

## 技术栈

| 类别 | 技术 |
|------|------|
| 框架 | Django 5.2 + Django REST Framework |
| 异步任务 | Celery + Redis |
| 实时通信 | Django Channels + WebSocket |
| 认证 | JWT (SimpleJWT) |
| 数据库 | SQLite（开发）/ PostgreSQL（生产） |
| 文档 | drf-spectacular（Swagger/OpenAPI） |
| 缓存 | Redis + Django Redis |
| 容器编排 | Kubernetes Python Client |
| 基础设施即代码 | Ansible Runner |
| 镜像管理 | Docker Registry API |

## 项目结构

```
backend/
├── apps/                        # 业务应用模块
│   ├── rbac_permission/         # 用户/角色/权限/菜单/审计日志
│   ├── host_management/          # 主机管理、平台接入、资源池
│   ├── task_management/          # Ansible 任务（Playbook 执行）
│   ├── pipeline_management/      # 流水线（DAG 可视化编排 + 定时调度）
│   ├── k8s_management/           # Kubernetes 多集群 + Helm 应用管理
│   ├── registry_management/      # Docker 镜像仓库
│   ├── approval_center/          # 发布审批工作流引擎
│   ├── credentials_management/   # 敏感凭据安全存储
│   ├── config_center/            # 配置中心（Redis/DB/MQ/日志配置）
│   └── system_management/        # 系统设置、监控
├── config/                       # Django 项目配置
│   ├── settings/
│   │   ├── base.py              # 基础配置（所有环境）
│   │   ├── development.py      # 开发环境覆盖
│   │   └── production.py       # 生产环境覆盖
│   ├── asgi.py                  # ASGI 配置（支持 WebSocket）
│   ├── celery.py               # Celery 异步任务配置
│   ├── routing.py              # Channels 路由
│   └── urls.py                 # 全局 URL 路由
├── utils/                       # 公共工具
│   ├── auth_views.py           # 认证视图（登录/刷新/登出）
│   ├── encryption.py            # 加密工具（Fernet 对称加密）
│   ├── exception_handler.py     # 全局异常处理
│   ├── middleware.py            # 中间件（审计日志）
│   ├── pagination.py            # 分页器
│   ├── rbac_permission.py       # SmartRBAC 权限核心
│   ├── renderers.py             # 全局 JSON 渲染器
│   ├── schema.py                # DRF Schema（权限感知）
│   ├── signals.py               # Django 信号定义
│   ├── config_manager.py        # 配置缓存与订阅者管理
│   ├── config_subscribers.py    # 内置配置订阅者
│   └── config_broadcast.py      # 多实例 Pub/Sub 广播
├── helm_charts/                 # Helm 部署 charts
├── docker-compose.yml          # 基础设施编排
└── manage.py
```

## 核心模块

### 流水线引擎（Pipeline Management）
- DAG 可视化：前端 ReactFlow 编排，后端存储 `graph_data`（nodes + edges）
- 节点类型：Ansible、GIT、Kaniko Build、HTTP、K8s Deploy
- 异步执行：Celery 分布式任务，Celery Beat 定时调度
- 实时日志：Channels WebSocket 推送，支持 ANSI 彩色日志流式输出
- 状态机：pending → running → success/failed/stopped
- **节点失败重试**：支持配置最大重试次数和重试间隔
- **版本历史**：每次保存自动创建版本快照，支持回滚
- **Webhook 触发器**：支持外部系统（GitHub/GitLab）通过 Webhook 触发流水线，自动验证签名
- **产物管理**：记录构建产物（Docker 镜像、JAR 包等）及版本历史

### Ansible 任务中心（Task Management）
- Playbook 解析与参数化执行
- 批量主机分组执行
- 实时执行日志流（WebSocket）
- 执行历史与状态追踪

### Kubernetes 多集群管理（K8s Management）
- 多集群接入（KubeConfig）
- Helm 3 一键部署/升级/回滚
- K8s 资源查看（Deployment/Service/Ingress/ConfigMap 等）
- 基于 `kubernetes-python-client` 原生 API

### 权限模型（SmartRBAC）
基于资源的细粒度 RBAC，精确到每个 API 接口和操作。

权限码格式：`{resource}:{action}`，如 `pipeline:template:edit`

核心实现：`utils/rbac_permission.py` → `SmartRBACPermission`

### 审批工作流（Approval Center）
- 可配置审批策略（多级审批、条件分支）
- 审批载荷快照（request payload capture）
- 强制签发（Override）机制
- 飞书/钉钉 WebHook 通知

### 配置中心（Config Center）
动态配置管理，支持 Redis/DB/MQ/日志等配置的热更新。

**核心功能：**
- 配置分类管理（Redis/数据库/消息队列/日志等）
- 配置项 CRUD（支持 string/int/float/bool/json 类型）
- 配置值加密存储（敏感信息）
- 热更新机制（修改配置自动生效，无需重启）
- 配置变更审计日志（记录变更历史）
- 配置回滚（回退到任意历史版本）
- 多实例 Pub/Sub 同步（Redis 广播）

**热更新流程：**
```
修改配置 → 清除缓存 → 通知订阅者 → 广播到其他实例 → 发送 Django 信号
```

**内置订阅者：**
| 订阅者 | 监听分类 | 处理逻辑 |
|--------|---------|---------|
| RedisConfigSubscriber | redis | 清除连接缓存 |
| LoggingConfigSubscriber | logging | 动态调整日志级别 |
| CacheConfigSubscriber | cache | 清除所有缓存 |

### 系统备份与恢复（Backup & Restore）
全量系统数据备份与恢复，支持跨实例迁移。

**备份内容：**
- 用户、角色、权限、菜单
- 主机、平台、环境、资源池
- 流水线模板、CI 环境
- K8s 集群配置、镜像仓库
- 凭据（加密存储）、配置中心
- 审批策略

**排除内容：**
- 执行日志、审计日志
- 流水线运行记录（PipelineRun）
- 用户密码（需重置）

**备份格式：** gzip 压缩的 JSON 文件（`.json.gz`）

## API 版本

所有接口以 `/api/v1/` 为前缀，版本控制通过 URL Path 实现。

主要模块路由：

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
| 凭据保险库 | `/api/v1/credentials/` |
| 配置中心 | `/api/v1/config/` |
| 系统 | `/api/v1/system/` |
| 系统备份 | `/api/v1/system/backup/` |

## 快速开始

### 环境要求

- Python 3.12+
- Redis 7+
- pnpm 10+（前端）

### 安装依赖

```bash
# 创建虚拟环境
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

# 安装依赖
pip install -e .

# 或使用 uv（推荐）
uv sync
```

### 配置环境变量

```bash
cp .env .env.local
# 编辑 .env.local，修改 DATABASE_URL、SECRET_KEY 等
```

`.env` 示例：

```env
SECRET_KEY=your-secret-key-here
DEBUG=True
ALLOWED_HOSTS=127.0.0.1,localhost
DATABASE_URL=sqlite:///db.sqlite3
CELERY_BROKER_URL=redis://127.0.0.1:6379/6
CORS_ALLOWED_ORIGINS=http://localhost:3000
```

### 数据库初始化

```bash
python manage.py migrate
python manage.py createsuperuser
```

### 启动服务

```bash
# 终端 1 - Django API
python manage.py runserver 0.0.0.0:8000

# 终端 2 - Celery Worker
celery -A config worker -l INFO

# 终端 3 - Celery Beat（定时任务调度器）
celery -A config beat -l INFO

# 终端 4 - Daphne（ASGI + WebSocket，生产推荐）
daphne -b 0.0.0.0 8000 config.asgi:application
```

### Docker Compose 启动（推荐）

```bash
docker compose up -d
```

## 权限码参考

| 资源 | 动作 | 权限码 |
|------|------|--------|
| 流水线模板 | 查看/新建/编辑/删除/执行 | `pipeline:template:view/add/edit/delete/execute` |
| 流水线运行 | 查看/停止 | `pipeline:run:view/stop` |
| CI 环境 | CRUD | `pipeline:ci_env:view/add/edit/delete` |
| 镜像仓库 | CRUD | `registry:docker:view/add/edit/delete` |
| 审批工单 | 查看/审批/强制签发 | `system:approval_ticket:view/approve` |
| 审计日志 | 查看 | `rbac:audit:view` |
| 凭据 | CRUD | `system:credential:view/add/edit/delete` |
| 系统监控 | 查看 | `system:monitor:view` |
| 用户管理 | CRUD | `rbac:user:view/add/edit/delete` |
| 角色管理 | CRUD | `rbac:role:view/add/edit/delete` |
| 菜单管理 | 查看/编辑 | `system:menu:view/edit` |

## 架构设计要点

### SmartRBAC 权限模型
每个 ViewSet 通过 `resource_code` 声明资源类型，Action 映射到具体操作：
```python
class PipelineViewSet(viewsets.ModelViewSet):
    resource_code = 'pipeline:template'

    @action(detail=True, methods=['post'])
    def execute(self, request, pk=None):
        # 权限码自动推导为 pipeline:template:execute
        ...
```

### 审计日志中间件
`utils/middleware.AuditLogMiddleware` 拦截所有写操作请求，自动记录用户/时间/IP/操作内容。

### 前端权限控制
后端返回用户权限码列表，前端 `hasPermission()` 函数做快速判断：
```typescript
// 未授权用户不会发送请求，按钮直接隐藏
enabled: !!token && hasPermission('pipeline:template:view'),
{hasPermission('pipeline:template:delete') && <Button>删除</Button>}
```

### WebSocket 实时日志
Channels + Redis 实现日志流推送，前端通过 `react-use-websocket` 消费：
```typescript
const { sendMessage, lastMessage } = useWebSocket(
  `ws://localhost:8000/ws/pipeline/${runId}/logs`
);
```

### 前端缓存策略
关键元数据（集群列表、用户信息）持久化到 `localStorage`，TTL 24 小时，`QueryPersistenceManager` 自动同步。

## License

Private - All Rights Reserved
