from apps.k8s_management.models import K8sCluster
from apps.task_management.models import AnsibleTask
from apps.pipeline_management.models import Pipeline
from apps.host_management.models import ResourcePool
from apps.registry_management.models import ImageRegistry
from apps.credentials_management.models import Credential
from utils.rbac_permission import get_user_data_scope

def get_authorized_resources(user, project_id=None):
    """
    获取用户有权访问的所有资源清单，用于注入 AI 上下文（支持项目隔离）
    """
    if not user or not user.is_authenticated:
        return {}

    resources = {
        'pipeline': {'model': Pipeline, 'label': '流水线'},
        'k8s_cluster': {'model': K8sCluster, 'label': 'K8s集群'},
        'ansible_task': {'model': AnsibleTask, 'label': 'Ansible任务'},
        'resource_pool': {'model': ResourcePool, 'label': '资源池'},
        'registry': {'model': ImageRegistry, 'label': '镜像仓库'},
        'credential': {'model': Credential, 'label': 'SSH凭据'},
    }

    auth_context = {}

    for r_type, info in resources.items():
        # 获取该资源类型的授权 ID 集合
        allowed_ids = get_user_data_scope(user, r_type, action_type='use')
        
        query = info['model'].objects.all()
        if "*" not in allowed_ids:
            query = query.filter(id__in=allowed_ids)
            
        # 如果指定了项目过滤，且模型中包含 project 字段
        if project_id:
            # 动态检查模型是否有 project_id 字段
            opts = info['model']._meta
            field_names = [f.name for f in opts.fields]
            if 'project' in field_names:
                query = query.filter(project_id=project_id)
        
        # 只提取 ID 和名称，减小 Token 消耗
        name_field = 'name'
        if r_type == 'pipeline':
            name_field = 'name'
        
        try:
            items = list(query.values('id', name_field))
            if items:
                auth_context[r_type] = {
                    'label': info['label'],
                    'items': items
                }
        except Exception:
            pass

    return auth_context
