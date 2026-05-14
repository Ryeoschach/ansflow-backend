# AnsFlow 后端 - 项目指令

> 本文件为 AnsFlow 后端项目提供持久上下文，覆盖所有在此目录中工作的开发者。

---

## 项目概述

AnsFlow 是一个企业级 DevOps 平台，后端采用 Django 5.2 + Django REST Framework 构建。集成了 **RAG 知识库**、**智能诊断**与 **SRE 自愈**能力。

### 技术栈

| 类别 | 技术 | 说明 |
|------|------|------|
| 框架 | Django 5.2 + DRF 3.16 | 核心 Web 框架 |
| AI 编排 | LangChain | RAG 链路与 LLM 交互驱动 |
| 向量数据库 | ChromaDB | 语义搜索本地存储 |
| 嵌入引擎 | FastEmbed | 轻量级 ONNX 运行时 |
| 大语言模型 | DeepSeek-V3 / OpenAI | 核心推理引擎 |
| 异步任务 | Celery 5.x + Redis | 分布式任务队列 |
| 实时通信 | Django Channels + WS | 流水线日志实时推送 |
| 容器编排 | Kubernetes Python Client | K8s 集群管理 |

### 目录结构

```
backend/
├── apps/                      # 业务应用模块
│   ├── ai_engine/             # AI 智能中心 (RAG + LLM)
│   ├── sre_management/        # SRE 智能运维 (告警/自愈)
│   ├── rbac_permission/       # 用户/角色/权限/审计
│   ├── pipeline_management/   # 流水线 (DAG + 调度)
│   ├── host_management/       # 主机管理
│   ├── task_management/       # Ansible 任务中心
│   ├── k8s_management/        # K8s 多集群 + Helm
│   ├── registry_management/   # Docker 镜像仓库
│   ├── approval_center/       # 审批工作流
│   ├── credentials_management/# 凭证存储中心
│   └── system_management/     # 系统设置与备份
├── config/                    # Django 项目配置
│   ├── settings/              # settings 分层 (base / development / production)
│   ├── asgi.py                # ASGI 配置 (WebSocket 支持)
│   ├── celery.py              # Celery 配置
│   └── routing.py             # Channels 路由
├── utils/                     # 共享工具类
├── chroma_db/                 # 向量数据库持久化目录
└── .model_cache/              # Embedding 模型缓存
```

### 核心 API 路由

所有 API 前缀: `/api/v1/`

| 模块 | 路由前缀 | 核心功能 |
|------|---------|---------|
| AI 助手 | `/api/v1/ai/` | 问答、诊断、RAG 管理 |
| SRE 运维 | `/api/v1/sre/` | 告警接收、自愈策略、诊断报告 |
| 流水线 | `/api/v1/pipelines/` | DAG 设计、执行控制、日志流 |
| K8s 管理 | `/api/v1/k8s/` | 集群资源、Helm 应用安装 |
| 认证 | `/api/v1/auth/` | JWT 登录、Token 刷新 |

---

## 开发规范

### 命名约定

- **Model**: PascalCase (如 `KnowledgeBase`)
- **ViewSet**: PascalCase 以 `ViewSet` 结尾
- **Serializer**: PascalCase 以 `Serializer` 结尾
- **Task**: snake_case (如 `process_document_embedding`)
- **权限码**: `snake_case` 格式 (如 `ai:knowledge:edit`)

### AI 与 RAG 规范

- **异步处理**: 文档向量化、AI 诊断必须通过 Celery 异步执行。
- **语义缓存**: 对高频请求优先进行向量匹配，相似度 > 0.95 时直接返回。
- **知识闭环**: 诊断结论应提供“存入知识库”接口，沉淀为 `human_verified_knowledge`。

### SRE 与自愈规范

- **状态强一致**: 使用 Django Signals 在流水线状态变更时自动同步更新告警事件状态。
- **操作溯源**: 所有自愈执行必须记录 `trigger_source` (auto/manual)。

### Model 规范

- 使用 `utils/base_model.py` 中的 `BaseModel` 作为基类。
- 敏感数据（密钥、Token）必须使用 `utils.encryption` 进行加密存储。

---

## 常用命令

| 命令 | 说明 |
|------|------|
| `python manage.py runserver` | 启动开发服务器 |
| `celery -A config worker -l INFO -P threads` | 启动 Worker (推荐 threads 模式以支持 AI 库) |
| `python manage.py spectacular --file schema.yml` | 更新 OpenAPI 定义 |

---

## 相关文档

- 详细开发准则: `@GEMINI.md`
- 业务逻辑定义: `@README.md`
