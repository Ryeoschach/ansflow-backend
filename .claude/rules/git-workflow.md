# Git 工作流规则

## 分支命名

- 功能: `feat/<module>/<description>`
- 修复: `fix/<module>/<description>`
- 热修: `hotfix/<description>`
- 示例: `feat/pipeline/dag-engine`, `fix/k8s/cluster-sync`

## 提交信息

使用 Conventional Commits 格式:

```
<type>(<scope>): <subject>

[optional body]

[optional footer]
```

### Type

- `feat`: 新功能
- `fix`: 修复 bug
- `docs`: 文档变更
- `style`: 代码格式（不影响功能）
- `refactor`: 重构
- `perf`: 性能优化
- `test`: 测试
- `chore`: 构建/工具变更

### 示例

```
feat(pipeline): add DAG execution engine

- implements advance_pipeline_engine task
- supports node retry on failure

Closes #123
```

## PR 规范

- PR 标题与提交信息格式一致
- Description 包含: 背景、变更内容、测试说明
- 需要至少一个 Reviewer 批准后才能合并

## 保护分支

- `main` / `master`: 受保护，禁止直接推送
- 所有变更通过 PR 合并
- CI 检查通过后才能合并
