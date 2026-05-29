from django.utils.deprecation import MiddlewareMixin
from apps.rbac_permission.models import Project, ProjectMember

class ProjectMiddleware(MiddlewareMixin):
    """
    项目/工作区识别中间件：
    从请求头 X-Project-ID / X-Project-Code 或 URL 查询参数 project_id / project_code 中读取当前激活的项目。
    """
    def process_request(self, request):
        # 1. 尝试使用 JWT 提前对 DRF 的请求进行身份校验
        if not hasattr(request, 'user') or request.user.is_anonymous:
            try:
                from rest_framework_simplejwt.authentication import JWTAuthentication
                auth = JWTAuthentication().authenticate(request)
                if auth:
                    request.user, request._auth = auth
            except Exception:
                pass

        # 2. 尝试从不同渠道提取项目标识
        project_id = request.headers.get('X-Project-ID') or request.GET.get('project_id')
        project_code = request.headers.get('X-Project-Code') or request.GET.get('project_code')
        
        project = None
        user = request.user if hasattr(request, 'user') and request.user.is_authenticated else None
        
        # 3. 查询对应的项目
        if project_id:
            project = Project.objects.filter(id=project_id).first()
        elif project_code:
            project = Project.objects.filter(code=project_code).first()
            
        # 4. 如果没传递任何项目，且用户已登录，尝试为用户加载一个默认或第一个关联的项目
        if not project and user:
            # 优先找 'default' code 的项目
            project = Project.objects.filter(code='default').first()
            # 如果 default 也没找到，找用户拥有的第一个项目
            if not project:
                first_membership = ProjectMember.objects.filter(user=user).select_related('project').first()
                if first_membership:
                    project = first_membership.project
                else:
                    # 如果用户也没有关联任何项目，且是超级管理员，可以直接关联 default 项目或第一个项目
                    project = Project.objects.first()

        # 5. 挂载到 request.project 上
        request.project = project
