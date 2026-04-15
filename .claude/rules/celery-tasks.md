---
paths:
  - "apps/**/tasks.py"
  - "config/celery.py"
---

# Celery 任务规则

## Task 命名

使用 snake_case，命名空间为 app 名称:

```python
@app.task(bind=True, name='pipeline:advance_engine')
def advance_pipeline_engine(self, run_id):
    ...
```

## Task 基础规范

```python
from celery import shared_task

@shared_task(bind=True)
def my_task(self, arg1, arg2):
    """
    任务描述

    Args:
        arg1: 参数1描述
        arg2: 参数2描述

    Returns:
        返回值描述
    """
    try:
        result = do_something(arg1, arg2)
        return {'status': 'success', 'result': result}
    except Exception as e:
        raise self.retry(exc=e, countdown=60, max_retries=3)
```

## 重试机制

```python
@app.task(bind=True, max_retries=3, default_retry_delay=60)
def execute_node(self, node_id):
    try:
        do_execute(node_id)
    except TemporaryError as e:
        raise self.retry(exc=e, countdown=60)
    except PermanentError as e:
        # 不重试，直接失败
        pass
```

## 定时任务

使用 `django-celery-beat`:

```python
from celery.schedules import crontab

@shared_task
def daily_cleanup():
    ...

# Celery Beat Schedule 配置在 Django settings 或数据库中
```

## Chaining Tasks

```python
from celery import chain

chain(
    task1.s(arg1),
    task2.s(),
    task3.s(task2_result),
)()
```

## Task 监控

- 使用 Flower 监控任务执行
- `django_celery_results` 存储任务结果

## 不要做的事

- **不要**在 Task 中使用 Django ORM 的 `get()` 而不处理 `DoesNotExist`
- **不要**在 Task 中打印敏感信息
- **不要**使用 `time.sleep()`，使用 Celery 的 `countdown`
- **不要**在 Task 中修改全局状态
