# AnsFlow Backend

企业级 DevOps 流水线平台后端，基于 Django 5.2 + DRF 构建。

**当前版本**: v1.2.2 (build: 2026-04-21)
**Demo**: https://ansflow.cyfee.com:10443
**默认账号**: admin / ansflow

---

## 技术栈

| 类别 | 技术 |
|------|------|
| 框架 | Django 5.2 + Django REST Framework 3.16 |
| 语言 | Python 3.12+ |
| ORM | Django ORM (SQLite dev / PostgreSQL prod) |
| 认证 | JWT via SimpleJWT（Cookie 存储） |
| 异步任务 | Celery 5.x + Redis |
| 实时通信 | Django Channels + WebSocket |
| 缓存 | Redis + django-redis |
| 容器编排 | Kubernetes Python Client |
| 基础设施即代码 | Ansible Runner |
| 镜像管理 | Docker Registry API |
| API 文档 | drf-spectacular (Swagger/OpenAPI) |

---

## 项目结构

```
backend/
├── apps/                          # 业务应用模块
│   ├── rbac_permission/            # 用户 / 角色 / 权限 / 菜单 / 审计日志
│   ├── host_management/            # 主机管理、平台接入、环境、资源池
│   ├── task_management/            # Ansible 任务（Playbook 执行）
│   ├── pipeline_management/        # 流水线 DAG 可视化编排 + 定时调度
│   ├── k8s_management/            # Kubernetes 多集群 + Helm 应用管理
│   ├── registry_management/        # Docker 镜像仓库 + 产物管理
│   ├── approval_center/            # 发布审批工作流引擎
│   ├── credentials_management/    # 敏感凭据安全存储（加密保险库）
│   ├── config_center/              # 配置中心（分类 / 项 / 变更审计）
│   └── system_management/          # 系统设置、健康检查、备份恢复
├── config/                         # Django 项目配置
│   ├── settings/
│   │   ├── base.py               # 基础配置（所有环境共用）
│   │   ├── development.py         # 开发环境覆盖
│   │   └── production.py          # 生产环境覆盖
│   ├── asgi.py                   # ASGI 配置（支持 WebSocket）
│   ├── celery.py                 # Celery 异步任务配置
│   ├── routing.py                # Channels 路由
│   └── urls.py                   # 全局 URL 路由
├── utils/                         # 公共工具
│   ├── auth_views.py             # 认证视图（登录 / 刷新 / 登出）
│   ├── encryption.py             # 加密工具（Fernet 对称加密）
│   ├── exception_handler.py      # 全局 DRF 异常处理
│   ├── middleware.py             # 中间件（审计日志）
│   ├── pagination.py             # 分页器
│   ├── rbac_permission.py        # SmartRBAC 权限核心
│   ├── config_manager.py         # 配置缓存与订阅者管理
│   ├── config_subscribers.py     # 内置配置订阅者（热更新）
│   └── config_broadcast.py       # 多实例 Pub/Sub 广播
├── helm_charts/                   # Helm 部署 chart
├── docker-compose.yml            # 基础设施编排
└── manage.py
```

---

## 快速开始

### 环境要求

- Python 3.12+
- Redis 7+
- Docker & Docker Compose（可选）

### 安装依赖

```bash
# 使用 uv（推荐）
uv sync

# 或 pip
pip install -e .
```

### 配置环境变量

```bash
cp .env.example .env
# 编辑 .env，修改 SECRET_KEY / DATABASE_URL / REDIS_URL 等
```

`.env` 关键配置：

```env
SECRET_KEY=your-secret-key-here
DEBUG=True
ALLOWED_HOSTS=127.0.0.1,localhost
DATABASE_URL=sqlite:///db.sqlite3
CELERY_BROKER_URL=redis://127.0.0.1:6379/6
CELERY_RESULT_BACKEND=redis://127.0.0.1:6379/7
CORS_ALLOWED_ORIGINS=http://localhost:3000
```

### 数据库初始化

```bash
uv run python manage.py migrate
uv run python manage.py createsuperuser
```

### 启动服务

```bash
# 终端 1 - Django API
uv run python manage.py runserver 0.0.0.0:8000

# 终端 2 - Celery Worker（执行流水线任务）
uv run celery -A config worker -l INFO

# 终端 3 - Celery Beat（定时调度器，支持数据库调度）
uv run celery -A config beat -l INFO --scheduler django_celery_beat.schedulers:DatabaseScheduler

# 终端 4 - Daphne ASGI + WebSocket（生产推荐）
daphne -b 0.0.0.0 8000 config.asgi:application
```

### Docker Compose 启动（推荐）

```bash
docker compose up -d
```

---

## 功能模块详解

---

### 1. 认证与账号（Authentication）

**路由**: `/api/v1/auth/`

| 接口 | 方法 | 说明 |
|------|------|------|
| `/api/v1/auth/login/` | POST | 登录，返回 Access + Refresh Token（写入 Cookie） |
| `/api/v1/auth/refresh/` | POST | 刷新 Access Token |
| `/api/v1/account/me/` | GET | 获取当前用户信息 |
| `/api/v1/account/menus/` | GET | 获取当前用户的菜单树 |

**登录请求体**:
```json
{ "username": "admin", "password": "ansflow" }
```

**响应**（Cookie 中写入 token，HttpOnly）：
```json
{ "access_token": "...", "refresh_token": "..." }
```

**Token 策略**:
- Access Token 有效期 60 分钟
- Refresh Token 有效期 7 天
- Token 存储在 HttpOnly Cookie 中，防止 XSS 窃取

---

### 2. 用户权限管理（RBAC）

**路由**: `/api/v1/users/` `/api/v1/roles/` `/api/v1/rbac/`

**Model**: `User / Role / Permission / Menu / AuditLog`

**核心特性**:

- **SmartRBAC 权限模型**：每个 ViewSet 通过 `resource_code` 声明资源类型，Action 自动映射到具体操作权限码
- **数据范围过滤**：`DataScopeMixin` 根据用户角色过滤可访问数据范围
- **审计日志**：所有写操作自动记录（用户 / 时间 / IP / 操作内容 / 变更前后数据）

**权限码格式**: `{resource}:{action}`

```
pipeline:template:view    # 查看流水线列表/详情
pipeline:template:add      # 新建流水线模板
pipeline:template:edit     # 编辑流水线配置
pipeline:template:delete   # 删除流水线模板
pipeline:template:execute  # 触发流水线执行
pipeline:run:view          # 查看执行历史
pipeline:run:stop         # 强制终止流水线实例
```

**用户数据结构**:
```json
{
  "id": 1,
  "username": "admin",
  "nickname": "管理员",
  "roles": ["超级管理员"],
  "permissions": ["pipeline:template:view", "pipeline:template:edit", "..."],
  "menus": [{ "name": "流水线", "path": "/pipeline", "children": [...] }]
}
```

**ViewSet 示例**:
```python
class PipelineViewSet(DataScopeMixin, viewsets.ModelViewSet):
    resource_code = 'pipeline:template'
    resource_type = 'pipeline'
    resource_owner_field = 'creator'
    # 权限码自动推导: list→view, create→add, update→edit, destroy→delete
```

---

### 3. 主机管理（Host Management）

**路由**: `/api/v1/hosts/` `/api/v1/environments/` `/api/v1/platforms/` `/api/v1/resource_pools/` `/api/v1/ssh_credentials/`

**Model**: `Host / Environment / Platform / ResourcePool / SshCredential`

**功能**:

- **平台接入**：管理多类主机平台（Linux/Windows/Database 等），通过 SSH Key 或密码认证
- **环境管理**：按环境分类主机（开发 / 测试 / 预发布 / 生产）
- **资源池**：主机分组，用于 Ansible 执行目标选择
- **SSH 凭据**：加密存储 SSH 私钥/密码，支持批量主机分组执行

**SSH 凭据加密**：私钥和密码使用 Fernet 对称加密存储在数据库，运行时解密用于 SSH 连接，**不在日志中打印任何明文凭据**。

---

### 4. Ansible 任务中心（Task Management）

**路由**: `/api/v1/tasks/` `/api/v1/executions/`

**Model**: `AnsibleTask / AnsibleExecution / TaskLog`

**功能**:

- **Playbook 管理**：存储并管理 Ansible Playbook，支持 `cmd`（即席命令）和 `playbook` 两种任务类型
- **参数化执行**：传入 `extravars` 变量，Playbook 动态渲染
- **目标主机**：通过 `resource_pool` 选择执行目标主机
- **实时日志**：执行过程中 TaskLog 实时写入，通过 WebSocket 推送前端
- **执行历史**：完整的执行记录（状态 / 开始时间 / 结束时间 / 摘要统计）

**执行流程**:
```
POST /api/v1/executions/ → 创建 AnsibleExecution (status=pending)
     ↓
Celery Task: run_ansible_task(execution_id)
     ↓
ansible_runner.run() → event_handler 回调写入 TaskLog
     ↓
执行完成 → status='success'/'failed' → WebSocket 推送结果
```

**执行状态**: `pending` → `running` → `success` / `failed`

---

### 5. 流水线引擎（Pipeline Management）

**路由**: `/api/v1/pipelines/` `/api/v1/pipeline_runs/` `/api/v1/pipeline/webhooks/` `/api/v1/pipeline/versions/` `/api/v1/ci_environments/`

**Model**: `Pipeline / PipelineRun / PipelineNodeRun / CIEnvironment / PipelineWebhook / PipelineVersion`

#### 5.1 流水线模板（Pipeline）

**核心概念**:

- **DAG 可视化编排**：前端 ReactFlow 画布编排节点和连线，后端存储 `graph_data`（nodes + edges JSON）
- **节点类型**：
  - `input`：流水线触发入口（无前置依赖的最上游节点）
  - `git_clone`：代码拉取（支持 GitHub/GitLab，指定分支）
  - `docker_build`：沙箱编译（挂载代码目录到 Docker 容器执行构建脚本）
  - `kaniko_build`：Kaniko 镜像构建（无需 Docker Daemon，直接推送到镜像仓库）
  - `ansible`：触发 Ansible 任务节点
  - `k8s_deploy`：Kubernetes 部署
  - `http_webhook`：HTTP 回调通知

**Pipeline 状态机**:
```
pending → running → success
                ↘→ failed → retry → running
                ↘→ cancelled
```

**节点执行状态**:
```
pending → running → success
                  ↘→ failed → （重试时）→ skipped（前置节点跳过）
```

**创建流水线**:
```json
POST /api/v1/pipelines/
{
  "name": "Java Maven 构建",
  "desc": "拉取代码 → Maven 打包 → 推送镜像",
  "graph_data": { "nodes": [...], "edges": [...] },
  "timeout": 3600,
  "is_active": true
}
```

#### 5.2 流水线执行（PipelineRun）

**触发方式**:

1. **手动触发**：`POST /api/v1/pipelines/{id}/execute/`
2. **Webhook 触发**：外部系统通过 Webhook 触发（GitHub push 事件等）
3. **定时触发**：Celery Beat 定时调度（需开启 `is_cron_enabled`）
4. **节点重试**：从失败节点重新执行 `POST /api/v1/pipeline_runs/{id}/retry/`

**执行流程（DAG 引擎）**:

```
advance_pipeline_engine(run_id)
  ├── 检查流水线状态（已终态直接返回）
  ├── 更新 run.status = running
  ├── 生成所有节点的 PipelineNodeRun 记录（首次执行）
  ├── DAG 拓扑遍历，寻找所有就绪节点（前置依赖全部 success/skipped）
  ├── apply_async(execute_pipeline_node) 下发节点任务
  └── 节点执行完成后回调引擎，继续下一轮调度
```

**节点执行（execute_pipeline_node）**:

```
execute_pipeline_node(node_run_id)
  ├── 加载节点配置（从 graph_data 获取 node_type 和 data）
  ├── 根据 node_type 分流：
  │   ├── git_clone    → git Python 库克隆代码到 {workspace_dir}/source/
  │   ├── docker_build → docker run 挂载 source_dir 执行构建脚本
  │   ├── kaniko_build → kaniko 镜像构建 + 推送
  │   ├── ansible      → run_ansible_task() 触发 Ansible 任务
  │   └── k8s_deploy   → kubectl apply 部署到 K8s
  ├── 更新 node_run.status / logs / output_data
  └── advance_pipeline_engine(run_id) 回调继续调度
```

**重试机制**:

- 支持从指定节点重试：`POST /api/v1/pipeline_runs/{id}/retry/` body: `{"start_node_id": "dndnode_2"}`
- 前置节点标记为 `skipped`，复用上次执行结果
- 重试节点使用父 run 的 workspace（避免重新 clone 代码）

#### 5.3 版本历史（PipelineVersion）

每次保存流水线模板自动创建版本快照，包含完整的 `graph_data`，支持一键回滚到任意历史版本。

```
POST /api/v1/pipelines/{id}/rollback/
Body: { "version_id": 3 }
```

#### 5.4 Webhook 触发器（PipelineWebhook）

支持外部系统通过 Webhook 触发流水线执行。

**Webhook 创建**:
```json
POST /api/v1/pipeline/webhooks/
{
  "pipeline": 5,
  "name": "GitHub Push Hook",
  "event_type": "push",
  "repository_url": "https://github.com/xxx/yyy",
  "branch_filter": "main",
  "secret_key": "my-webhook-secret",
  "is_active": true
}
```

**触发地址**: `/api/v1/pipeline/webhooks/{id}/trigger/`

**签名验证（三种方式，按优先级）**:

1. **GitHub 签名**（`X-Hub-Signature-256`）：GitHub 官方 Webhook 格式
2. **AnsFlow 签名**（`X-AnsFlow-Signature` + `X-AnsFlow-Timestamp`）：HMAC-SHA256 + 时间戳防重放
3. **明文 Secret**（`?secret=xxx` 或 body secret）：向后兼容旧版

**GitHub 签名验证示例**:
```bash
# 在 GitHub Webhook 设置中填入 secret
# GitHub 自动使用 HMAC-SHA256 签名发送
X-Hub-Signature-256: sha256=abc123...
```

**AnsFlow 自定义签名**:
```bash
# 签名计算: HMAC-SHA256(secret, "{timestamp}.{body}")
timestamp=$(date +%s)
body='{"event":"push"}'
signature=$(echo -n "${timestamp}.${body}" | openssl dgst -sha256 -hmac "secret" | cut -d' ' -f2)

curl -X POST "https://your-domain/api/v1/pipeline/webhooks/1/trigger/" \
  -H "X-AnsFlow-Timestamp: ${timestamp}" \
  -H "X-AnsFlow-Signature: sha256=${signature}" \
  -d "${body}"
```

#### 5.5 CI 环境（CIEnvironment）

管理流水线节点的执行环境镜像，例如：

```json
{
  "name": "java-maven",
  "image": "maven:3-eclipse-temurin-17",
  "type": "java",
  "description": "Java 17 + Maven 3 构建环境"
}
```

---

### 6. Kubernetes 多集群管理（K8s Management）

**路由**: `/api/v1/k8s/`

**Model**: `K8sCluster`

**功能**:

- **多集群接入**：通过 KubeConfig 文件接入多个 K8s 集群
- **Helm 应用管理**：部署 / 升级 / 回滚 Helm Chart
- **资源查看**：Deployment / Service / Ingress / ConfigMap / Secret / Pod 等
- **健康检查**：实时检测集群连接状态（5 秒超时，不阻塞页面渲染）

**Helm 部署**:
```json
POST /api/v1/k8s/helm/
{
  "cluster": 1,
  "namespace": "production",
  "release_name": "my-app",
  "chart_url": "https://charts.bitnami.com/bitnami/wordpress-20.0.0.tgz",
  "values": {
    "image.repository": "my-registry.com/my-app",
    "image.tag": "v1.0.0"
  }
}
```

---

### 7. 镜像仓库与产物管理（Registry Management）

**路由**: `/api/v1/image_registries/` `/api/v1/artifacts/` `/api/v1/artifact-versions/`

**Model**: `ImageRegistry / Artifact / ArtifactVersion`

**功能**:

- **镜像仓库**：管理 Docker Registry（Docker Hub / 私有仓库），支持认证
- **产物记录**：记录构建产物（Docker 镜像 / JAR 包等）及版本历史
- **版本追溯**：每次构建生成产物快照，可追溯历史版本

**Artifact 数据结构**:
```json
{
  "name": "simple-java-maven-app",
  "artifact_type": "jar",
  "registry": 1,
  "current_version": "v1.0.0",
  "description": "Java Maven 示例应用"
}
```

---

### 8. 审批工作流（Approval Center）

**路由**: `/api/v1/approval_policies/` `/api/v1/approval_tickets/`

**Model**: `ApprovalPolicy / ApprovalTicket`

**功能**:

- **审批策略**：可配置多级审批、条件分支、审批人规则
- **工单管理**：创建 / 审批 / 拒绝 / 强制签发（Override）
- **载荷快照**：自动捕获触发审批的完整请求体（request payload）
- **通知推送**：支持飞书 / 钉钉 Webhook 通知
- **安全策略**：可对接 ProxyApprovalEngine，自动将高风险操作转为工单审批

**审批流程**:
```
触发审批 → 创建工单（pending）→ 审批人处理（approved/rejected/overridden）→ 执行后续操作
```

**强制签发（Override）**：超级管理员可跳过审批直接执行，保留强制签发记录。

---

### 9. 配置中心（Config Center）

**路由**: `/api/v1/config/categories/` `/api/v1/config/items/` `/api/v1/config/change-logs/`

**Model**: `ConfigCategory / ConfigItem / ConfigChangeLog`

**功能**:

- **分类管理**：将配置按用途分组（Redis / 数据库 / 消息队列 / 日志 / 通知 等）
- **配置项 CRUD**：支持 `string` / `int` / `float` / `bool` / `json` 五种类型
- **敏感值加密**：敏感配置项自动加密存储
- **热更新**：修改配置自动生效，无需重启服务
- **变更审计**：完整记录每次配置变更（变更人 / 时间 / 变更前后值）
- **配置回滚**：可回退到任意历史版本

**热更新机制**:

```
修改配置 → ConfigCache 失效 → ConfigSubscriber 收到通知 → 各模块重载配置
```

**内置订阅者**:

| 订阅者 | 监听分类 | 处理逻辑 |
|--------|---------|---------|
| RedisConfigSubscriber | redis | 清除 Redis 连接缓存 |
| LoggingConfigSubscriber | logging | 动态调整日志级别 |
| CacheConfigSubscriber | cache | 清除 Django 缓存 |
| NotificationConfigSubscriber | notification | 清除通知配置缓存 |

**通知配置示例**（Config Category: `notification`）:

| Key | 类型 | 说明 |
|-----|------|------|
| `enabled` | bool | 是否启用通知 |
| `level` | string | 通知级别（info/warn/error） |
| `feishu.enabled` | bool | 飞书通知开关 |
| `feishu.webhook_url` | string | 飞书 Webhook URL |
| `dingtalk.enabled` | bool | 钉钉通知开关 |
| `dingtalk.webhook_url` | string | 钉钉 Webhook URL |
| `frontend_url` | string | 前端访问地址（用于生成通知链接） |

---

### 10. 凭据保险库（Credentials Management）

**路由**: `/api/v1/credentials/`

**Model**: `Credential`

**功能**:

- **加密存储**：使用 Fernet 对称加密算法加密存储敏感凭据（API Key / 密码 / Token 等）
- **分类管理**：支持按类型（api_key/password/token/certificate）分类
- **环境隔离**：可关联特定环境（开发/测试/生产），不同环境使用不同凭据
- **审计日志**：所有凭据访问记录在审计日志中

**凭据结构**:
```json
{
  "name": "GitHub API Token",
  "credential_type": "api_key",
  "username": "my-github-user",
  "encrypted_value": "gAAAAABh...",  // 加密存储
  "env": "production",
  "description": "用于 GitHub Webhook 签名验证"
}
```

---

### 11. 系统管理与监控（System Management）

**路由**: `/api/v1/system/health/` `/api/v1/system/dashboard/` `/api/v1/system/backup/`

#### 11.1 健康检查（SystemHealthViewSet）

**路由**: `/api/v1/system/health/status/`

**检查项**:

| 检查项 | 超时 | 说明 |
|--------|------|------|
| Celery | 3s | `app.control.inspect()` 获取 worker 状态 |
| Redis | 2s | Redis PING |
| Database | 2s | Django ORM 执行 `SELECT 1` |
| K8s 集群 | 5s | `curl --max-time 5 {server}/version/` |

> 注意：每个检查项独立超时，单个故障不阻塞其他检查，页面不会因为一个集群超时而全屏 loading。

#### 11.2 系统备份（BackupViewSet）

**路由**: `/api/v1/system/backup/` `/api/v1/system/backup/restore/`

**备份内容**:

- 用户、角色、权限、菜单
- 主机、平台、环境、资源池
- 流水线模板、CI 环境
- K8s 集群配置、镜像仓库
- 凭据（加密存储）、配置中心数据
- 审批策略

**排除内容**:

- 执行日志、审计日志（数据量大且无需迁移）
- 流水线运行记录（PipelineRun）
- 用户密码（迁移后需重置）

**备份格式**: gzip 压缩的 JSON 文件（`.json.gz`）

#### 11.3 仪表盘（DashboardViewSet）

**路由**: `/api/v1/system/dashboard/stats/`

聚合展示系统关键指标（流水线总数 / 执行次数 / 成功率等）。

---

### 12. 审计日志（Audit Log）

**路由**: `/api/v1/audit-logs/`

**Model**: `AuditLog`

**记录内容**:

- 操作人、时间、IP 地址
- 操作类型（增/删/改）
- 资源类型和具体操作
- 变更前后数据快照（变更类操作）
- 请求路径、请求方法、响应状态码

**中间件**: `utils/middleware.AuditLogMiddleware` 全局拦截所有写操作请求。

---

## API 路由总览

所有接口以 `/api/v1/` 为前缀。

| 模块 | 路由前缀 | 核心功能 |
|------|---------|---------|
| 认证 | `/api/v1/auth/` | 登录 / 刷新 Token |
| 账号 | `/api/v1/account/` | 当前用户信息 / 菜单树 |
| 用户管理 | `/api/v1/users/` | 用户 CRUD |
| 角色管理 | `/api/v1/roles/` | 角色 CRUD + 权限分配 |
| 权限管理 | `/api/v1/system/permissions/` | 权限码查看 |
| 菜单管理 | `/api/v1/system/menus/` | 菜单树管理 |
| 主机管理 | `/api/v1/hosts/` | 主机 CRUD |
| 环境管理 | `/api/v1/environments/` | 环境 CRUD |
| 平台管理 | `/api/v1/platforms/` | 平台 CRUD |
| 资源池 | `/api/v1/resource_pools/` | 资源池 CRUD |
| SSH 凭据 | `/api/v1/ssh_credentials/` | SSH 凭据 CRUD |
| Ansible 任务 | `/api/v1/tasks/` | 任务模板 CRUD |
| Ansible 执行记录 | `/api/v1/executions/` | 执行历史 / 状态查看 |
| 流水线 | `/api/v1/pipelines/` | 模板 CRUD / 执行 / 回滚 |
| 流水线运行 | `/api/v1/pipeline_runs/` | 运行记录 / 停止 / 重试 |
| Webhook | `/api/v1/pipeline/webhooks/` | Webhook 配置 / 触发 |
| 流水线版本 | `/api/v1/pipeline/versions/` | 版本历史 / 回滚 |
| CI 环境 | `/api/v1/ci_environments/` | CI 环境镜像管理 |
| K8s 集群 | `/api/v1/k8s/` | 集群接入 / Helm 部署 |
| 镜像仓库 | `/api/v1/image_registries/` | Registry 管理 |
| 产物管理 | `/api/v1/artifacts/` | 产物记录 / 版本 |
| 审批策略 | `/api/v1/approval_policies/` | 审批策略 CRUD |
| 审批工单 | `/api/v1/approval_tickets/` | 工单查看 / 审批 |
| 凭据保险库 | `/api/v1/credentials/` | 凭据 CRUD（加密） |
| 配置分类 | `/api/v1/config/categories/` | 配置分类 CRUD |
| 配置项 | `/api/v1/config/items/` | 配置项 CRUD / 热更新 |
| 配置变更日志 | `/api/v1/config/change-logs/` | 变更历史 / 回滚 |
| 审计日志 | `/api/v1/audit-logs/` | 操作审计查看 |
| 系统健康 | `/api/v1/system/health/` | 健康检查（独立超时） |
| 系统仪表盘 | `/api/v1/system/dashboard/` | 统计指标 |
| 系统备份 | `/api/v1/system/backup/` | 备份 / 恢复 |

---

## WebSocket 实时通信

**路由**: `/ws/pipeline/{run_id}/logs/`

通过 Django Channels 实现流水线执行日志实时推送，前端通过 WebSocket 消费：

```typescript
const { sendMessage, lastMessage } = useWebSocket(
  `ws://localhost:8000/ws/pipeline/134/logs`
);

// 接收日志消息
useEffect(() => {
  if (lastMessage) {
    const data = JSON.parse(lastMessage.data);
    if (data.type === 'log') {
      appendLog(data.content); // 追加到日志面板
    } else if (data.type === 'status') {
      updatePipelineStatus(data); // 更新流水线状态
    }
  }
}, [lastMessage]);
```

---

## 权限码参考

| 资源 | 动作 | 权限码 |
|------|------|--------|
| 流水线模板 | 查看/新建/编辑/删除/执行 | `pipeline:template:view/add/edit/delete/execute` |
| 流水线运行 | 查看/停止 | `pipeline:run:view/stop` |
| 流水线版本 | 查看/回滚 | `pipeline:version:view/rollback` |
| Webhook | 查看/新建/编辑/删除/触发 | `pipeline:webhook:view/add/edit/delete/trigger` |
| CI 环境 | 查看/新建/编辑/删除 | `pipeline:ci_env:view/add/edit/delete` |
| 主机 | 查看/新建/编辑/删除 | `host:host:view/add/edit/delete` |
| 环境 | 查看/新建/编辑/删除 | `host:env:view/add/edit/delete` |
| 资源池 | 查看/新建/编辑/删除 | `host:resource_pool:view/add/edit/delete` |
| SSH 凭据 | 查看/新建/编辑/删除 | `host:ssh_credential:view/add/edit/delete` |
| Ansible 任务 | 查看/新建/编辑/删除 | `task:ansible_task:view/add/edit/delete` |
| 镜像仓库 | 查看/新建/编辑/删除 | `registry:docker:view/add/edit/delete` |
| 审批工单 | 查看/审批/强制签发 | `system:approval_ticket:view/approve` |
| 审计日志 | 查看 | `rbac:audit:view` |
| 凭据 | 查看/新建/编辑/删除 | `system:credential:view/add/edit/delete` |
| 系统监控 | 查看 | `system:monitor:view` |
| 用户管理 | 查看/新建/编辑/删除 | `rbac:user:view/add/edit/delete` |
| 角色管理 | 查看/新建/编辑/删除 | `rbac:role:view/add/edit/delete` |
| 菜单管理 | 查看/编辑 | `system:menu:view/edit` |
| 配置项 | 查看/编辑 | `config:config_item:view/edit` |
| K8s 集群 | 查看/新建/编辑/删除 | `k8s:cluster:view/add/edit/delete` |

---

## 架构设计要点

### SmartRBAC 权限模型

每个 ViewSet 通过 `resource_code` 声明资源类型，Action 自动映射到权限码后缀：

```python
class PipelineViewSet(DataScopeMixin, viewsets.ModelViewSet):
    resource_code = 'pipeline:template'    # → 权限码前缀
    resource_type = 'pipeline'              # → 数据范围过滤类型
    resource_owner_field = 'creator'        # → 所有者字段（数据权限豁免）

    @action(detail=True, methods=['post'])
    def execute(self, request, pk=None):
        # 权限码自动推导为 pipeline:template:execute
        ...
```

### 数据范围过滤（DataScopeMixin）

用户只能看到：

- 自己创建的数据
- 角色数据策略允许访问的范围内数据

### 审计日志中间件

`utils/middleware.AuditLogMiddleware` 全局拦截所有 `POST` / `PUT` / `PATCH` / `DELETE` 请求，自动记录用户 / 时间 / IP / 操作内容 / 变更前后数据快照。

### 通知系统

支持飞书 / 钉钉 Webhook 通知，配置通过 ConfigCenter 管理（热更新）。

触发场景：

- 流水线启动
- 流水线执行完成（成功 / 失败）
- 审批工单创建
- 审批结果（通过 / 拒绝）

### 定时调度

使用 `django_celery_beat` + `DatabaseScheduler`，流水线定时调度通过数据库管理，支持在页面动态启停定时任务。

---

## License

Private - All Rights Reserved
