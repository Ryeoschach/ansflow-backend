# 安全规则

## 敏感信息

- **禁止**在代码中硬编码:
  - 密码、Secret Key
  - API Token
  - 数据库连接字符串
  - 私钥/证书
- 所有敏感配置通过环境变量或 `.env` 文件
- `.env` 文件**不提交**到版本控制

## 输入验证

- 所有 API 输入必须通过 Serializer 验证
- 不要信任用户输入，直接拼接 SQL 或文件路径
- 使用 ORM 的参数化查询

## 权限检查

- 前端权限检查是 UX 层面的，后端必须强制验证
- 使用 `SmartRBACPermission`，不要绕过
- 敏感操作需要额外验证（如二次确认）

## 审计日志

- 使用 `utils.middleware.AuditLogMiddleware`
- 记录所有状态变更操作
- 包含操作人、时间、变更前后数据

## 密码处理

- 使用 Django 内置 `make_password` / `check_password`
- 不要自己实现加密逻辑

## XSS / Injection

- DRF Serializer 自动处理 XSS
- 文件上传验证文件类型和大小
- 使用 `html.escape()` 处理原始 HTML 显示

## 不要做的事

- **不要**在日志中打印敏感信息（密码、token）
- **不要**使用 `eval()` 或 `exec()`
- **不要**信任 `request.data` 而不验证
