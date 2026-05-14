# GEMINI.md - AnsFlow 后端开发准则

本文件规定了 Gemini CLI 在 AnsFlow 后端项目中的核心开发指令。这些准则具有最高优先级，优于默认设置。

## 1. 项目概况
AnsFlow 是一个企业级 DevOps 平台后端。
- **框架：** Django 5.2 + DRF 3.16
- **核心：** SmartRBAC (权限系统), DAG 引擎 (流水线), Celery (异步任务), Redis (缓存/消息代理)。

## 2. 核心开发规范

### 2.1 模型 (Models) 规范
- **基类：** 所有业务模型必须继承 `utils.base_model.BaseModel`。
- **字段：** 必须包含 `id`, `create_time`, `update_time`（由基类提供）。
- **命名：** 使用 PascalCase（如 `PipelineRun`）。
- **严禁：** 除非明确要求，禁止使用原生 SQL。优先使用 Django ORM。

### 2.2 接口 (API & ViewSet) 规范
- **命名：** ViewSet 必须以 `ViewSet` 结尾，Serializer 必须以 `Serializer` 结尾。
- **SmartRBAC 集成：** 每个 ViewSet 必须声明：
    - `permission_classes = [SmartRBACPermission]`
    - `resource_code`: 权限码前缀（如 `pipeline:template`）。
    - `resource_type`: 数据权限过滤的资源类型。
    - `resource_owner_field`: 通常为 `creator` 或 `user`。
- **数据隔离：** 必须使用 `DataScopeMixin` 确保多租户/数据隔离安全。
- **文档：** 使用 `drf-spectacular` 装饰器（`@extend_schema`）标注所有接口。
- **路由：** 所有 ViewSet 统一在 `config/api_router.py` 注册。

### 2.3 序列化与校验 (Serializers)
- **输入安全：** 严禁直接信任 `request.data`。所有输入必须通过 Serializer 校验。
- **敏感字段：** 密码、密钥、Token 必须设置 `write_only=True`。
- **逻辑分离：** Serializer 仅负责数据转换，业务逻辑应放在 ViewSet、Service 层或 Celery Task 中。

### 2.4 备份与还原 (Backup & Restore)
- **扩展规范：** 新增业务模型时，必须更新 `apps/system_management/backup.py`：
    - 在 `MODEL_INFOS` 中注册。
    - 定义 `fk_fields` 和 `m2m_fields` 以支持三阶段导入。
    - 将敏感字段加入 `get_encrypted_field_names()` 防止明文导出。
    - 指定 `unique_fields` 确保导入的幂等性。

### 2.5 RAG 与 AI 规范
- **知识闭环：** AI 模块新增功能必须考虑如何将结果沉淀为知识。手动导出至 RAG 时，元数据必须包含 `source` 和 `type: human_verified_knowledge`。
- **物理同步：** 删除 `KnowledgeDocument` 时必须同步物理删除 `file_path` 指向的文件，并调用 `RAGService.delete_document` 彻底清理向量库。
- **语义缓存：** 针对高频且重复的 AI 诊断请求，优先在 `RAGService` 中实现基于向量相似度的语义检索缓存（阈值建议 0.9+），以降低 LLM 调用成本。
- **文档解析：** PDF 解析优先使用 `PyMuPDF`。针对扫描件或图片型 PDF，系统会自动触发基于 `Tesseract` 的 OCR 解析。开发时应确保环境已安装 `tesseract-ocr` 及其语言包。

### 2.6 SRE 与自愈规范
- **状态同步：** 告警自愈流水线必须实现实时的状态同步。使用 Django Signals 监听 `PipelineRun` 的状态变化，并即时更新关联的 `AlertEvent`。
- **可观测性：** 自愈任务必须在 `AuditLog` 中标记为 `system_auto_triggered`，并在 UI 上显示清晰的执行进度条。
- **降级保护：** 所有的自愈策略必须包含 `cooldown` 冷却时间配置，防止在故障震荡期间产生并发冲突或执行风暴。

### 2.7 跨模块状态同步 (Signals)
- **解耦原则：** 跨应用（App）的状态联动（如流水线状态同步至告警事件）必须通过 Django Signals 实现，禁止在 ViewSet 或 Task 中直接耦合其他应用的 Model。
- **性能：** Signal 处理逻辑应保持轻量，耗时操作应进一步下发至 Celery。

## 3. 工程化与安全

### 3.1 安全准则
- **严禁硬编码：** 绝对禁止在代码中硬编码密钥、API Token 或数据库连接串。使用 `.env` 或环境变量。
- **加密存储：** 敏感数据（如 SSH 私钥）在数据库中必须使用 `utils.encryption` 进行 Fernet 对称加密。
- **审计日志：** 所有变更操作（POST/PUT/PATCH/DELETE）通过 `AuditLogMiddleware` 记录，确保逻辑不绕过中间件。

### 3.2 Git 工作流
- **分支命名：** 功能 `feat/<module>/<desc>`, 修复 `fix/<module>/<desc>`。
- **提交规范：** 使用 Conventional Commits（如 `feat(pipeline): ...`, `fix(k8s): ...`）。
- **提交确认：** 始终先提出中文 commit message 草案，在用户确认后执行。

## 4. 执行流程
1. **研究 (Research)：** 查看 `apps/` 目录下的 `models.py` 和 `serializers.py` 以对齐现有模式。
2. **策略 (Strategy)：** 按照“模型 -> 序列化器 -> 视图 -> 路由”的顺序规划变更。
3. **执行 (Execution)：** 使用 `replace` 进行外科手术式代码修改。
4. **验证 (Validation)：** 确保新操作的 SmartRBAC 权限码已正确推导并生效。

---
