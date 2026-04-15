---
paths:
  - "config/settings/*.py"
  - "manage.py"
---

# 配置管理规则

## Settings 分层

```
config/settings/
├── base.py        # 基础配置，所有环境共享
├── development.py # 开发环境覆盖
└── production.py  # 生产环境覆盖
```

## 环境变量

使用 `django-environ`:

```python
import environ

env = environ.Env()

SECRET_KEY = env('SECRET_KEY')
DEBUG = env.bool('DEBUG', default=False)
ALLOWED_HOSTS = env.list('ALLOWED_HOSTS', default=[])

DATABASES = {
    'default': env.db('DATABASE_URL', default='sqlite:///db.sqlite3')
}

REDIS_URL = env('REDIS_URL', default='redis://localhost:6379/0')
```

## .env 文件

```bash
# .env (不提交!)
SECRET_KEY=your-secret-key
DEBUG=True
ALLOWED_HOSTS=localhost,127.0.0.1
DATABASE_URL=sqlite:///db.sqlite3
REDIS_URL=redis://localhost:6379/0
```

## 生产环境

- `DEBUG=False`
- `ALLOWED_HOSTS` 必须配置
- 使用 PostgreSQL
- 使用 Redis 缓存
- 配置日志输出到文件

## 不要做的事

- **不要**在 settings 中硬编码值
- **不要**提交 `.env` 文件
- **不要**在代码中使用 `print()`，使用 logging
