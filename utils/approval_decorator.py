from functools import wraps
from apps.approval_center.engine import ProxyApprovalEngine

def require_approval(resource_type: str, action_title_prefix: str = "高危操作"):
    """
    DRF 视图/动作的审批拦截装饰器
    用法:
        @action(detail=True, methods=['post'])
        @require_approval(resource_type='pipeline:run', action_title_prefix='运行流水线模板')
        def execute(self, request, pk=None):
            ...
    """
    def decorator(view_func):
        @wraps(view_func)
        def _wrapped_view(viewset_instance, request, *args, **kwargs):
            # 尝试通过视图类的方法获取环境
            environment = None
            if hasattr(viewset_instance, 'get_approval_environment'):
                environment = viewset_instance.get_approval_environment(request, *args, **kwargs)

            # 解析目标 ID (优先取 URL 中的 pk)
            target_id = kwargs.get('pk') or request.data.get('id')
            action_title = f"{action_title_prefix} #{target_id}" if target_id else action_title_prefix

            # 执行拦截检查
            is_blocked, approval_res = ProxyApprovalEngine.intercept_if_needed(
                request, 
                resource_type=resource_type, 
                action_title=action_title,
                target_id=str(target_id) if target_id else None,
                environment=environment
            )
            
            if is_blocked:
                return approval_res

            # 若放行，则执行原视图逻辑
            return view_func(viewset_instance, request, *args, **kwargs)
            
        return _wrapped_view
    return decorator
