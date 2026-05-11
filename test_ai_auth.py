import os
import django
import json

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.base')
django.setup()

from django.contrib.auth import get_user_model
from apps.rbac_permission.models import Role, Permission, DataPolicy
from apps.pipeline_management.models import Pipeline
from apps.ai_engine.utils import get_authorized_resources

User = get_user_model()

def test_ai_permission_filtering():
    print("开始测试 AI 权限过滤...")
    
    # 1. 准备数据
    test_user, _ = User.objects.get_or_create(username="ai_test_user")
    test_user.roles.clear()
    
    # 创建两条流水线
    p1, _ = Pipeline.objects.get_or_create(name="Visible Pipeline")
    p2, _ = Pipeline.objects.get_or_create(name="Hidden Pipeline")
    
    # 创建角色并只授权 p1
    role, _ = Role.objects.get_or_create(name="AI Tester Role")
    test_user.roles.add(role)
    
    DataPolicy.objects.update_or_create(
        role=role, 
        resource_type='pipeline', 
        action_type='use',
        defaults={'authorized_ids': [p1.id]}
    )
    
    # 2. 执行权限感知工具类
    auth_resources = get_authorized_resources(test_user)
    
    # 3. 验证结果
    pipelines = auth_resources.get('pipeline', {}).get('items', [])
    pipeline_ids = [p['id'] for p in pipelines]
    
    print(f"用户可见流水线 ID 列表: {pipeline_ids}")
    
    if p1.id in pipeline_ids and p2.id not in pipeline_ids:
        print("✅ 测试通过：AI 成功过滤了未授权资源。")
    else:
        print("❌ 测试失败：AI 输出了未授权资源或未过滤。")

if __name__ == "__main__":
    test_ai_permission_filtering()
