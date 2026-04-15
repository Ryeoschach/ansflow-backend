# 测试规则

## 测试框架

- pytest + pytest-django
- Factory Boy (测试数据)
- pytest-cov (覆盖率)

## 测试文件组织

```
apps/
├── pipeline/
│   ├── test_models.py
│   ├── test_views.py
│   ├── test_tasks.py
│   └── factories.py        # Factory Boy 工厂
```

## 编写规范

### Model 测试

```python
import pytest
from apps.pipeline.models import Pipeline

@pytest.mark.django_db
def test_pipeline_creation():
    pipeline = Pipeline.objects.create(
        name='Test Pipeline',
        description='Test'
    )
    assert pipeline.id is not None
    assert pipeline.create_time is not None
```

### View 测试

```python
from rest_framework.test import APIClient

@pytest.mark.django_db
def test_pipeline_list(api_client, pipeline):
    response = api_client.get('/api/v1/pipelines/')
    assert response.status_code == 200
    assert response.data['count'] >= 1
```

### Task 测试

```python
from unittest.mock import patch

@pytest.mark.django_db
@patch('apps.pipeline.tasks.execute_node')
def test_advance_engine(mock_execute, pipeline_run):
    advance_pipeline_engine(pipeline_run.id)
    mock_execute.assert_called_once()
```

## Fixture 规范

使用 pytest-django 的 `django_db` mark 和 conftest.py:

```python
# conftest.py
import pytest
from rest_framework.test import APIClient

@pytest.fixture
def api_client():
    return APIClient()

@pytest.fixture
def authenticated_client(api_client, user):
    api_client.force_authenticate(user=user)
    return api_client
```

## 覆盖率目标

- 核心业务逻辑: 80%+
- Model 层: 90%+
- View/Serializer 层: 70%+
