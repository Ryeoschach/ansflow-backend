import json
import threading
import time
from django.utils.deprecation import MiddlewareMixin
from apps.rbac_permission.models import AuditLog, Permission


class AuditLogMiddleware(MiddlewareMixin):
    """
    审计日志中间件：捕捉所有写操作请求
    """

    # 不监控的路径列表（精确匹配）
    EXCLUDE_PATHS = [
        '/api/v1/auth/refresh/',  # Token 刷新接口
        '/api/v1/auth/login/',    # 登录接口（密码类）
        '/api/v1/auth/social/github/',    # GitHub 登录
        '/api/v1/auth/social/wechat/',    # 微信登录
        '/api/v1/auth/ldap/login/',       # LDAP 登录
    ]

    def process_request(self, request):
        # 记录请求到达时的时间，用于计算后续耗时
        request.start_time = time.time()
        
        # 提前安全地读取并缓存请求体，防止 DRF 视图消费 body 之后，导致在此处读出空套接字
        try:
            request._cached_body = request.body
        except Exception:
            request._cached_body = b''

    def process_view(self, request, view_func, view_args, view_kwargs):
        # 在路由匹配完，准备进入 DRF View 逻辑（修改数据）前拦截
        if request.method in ['PUT', 'PATCH', 'DELETE']:
            try:
                pk = view_kwargs.get('pk') or view_kwargs.get('id')
                view_class = getattr(view_func, 'cls', None)
                if view_class and pk and hasattr(view_class, 'queryset'):
                    # 强行抢在修改之前把老数据拿出来
                    obj = view_class.queryset.model.objects.filter(pk=pk).first()
                    if obj:
                        # 尝试使用该视图绑定的 Sequence 序列化器进行最完美的 Snapshot (保留结构)
                        try:
                            view_instance = view_class()
                            serializer_class = view_instance.get_serializer_class()
                            request._old_data = serializer_class(obj).data
                        except Exception:
                            # 降级：如果动态获取序列化器失败，则直接用 ORM 的 model_to_dict
                            from django.forms.models import model_to_dict
                            request._old_data = model_to_dict(obj)
            except Exception:
                pass # 这个钩子绝对不能影响主线业务，失败即放弃快照
        return None

    def process_response(self, request, response):
        # 过滤：审计写操作 (POST, PUT, PATCH, DELETE)
        if request.method not in ['POST', 'PUT', 'PATCH', 'DELETE']:
            return response

        # 过滤：排除路径
        if request.path in self.EXCLUDE_PATHS:
            return response

        # 截取结束时间
        end_time = time.time()
        
        # 提取数据（启动新线程保存，避免阻塞主流程）
        try:
            threading.Thread(target=self.save_log, args=(request, response, end_time)).start()
        except Exception:
            pass  # 防止生产环境因为日志记录失败导致业务中断
        return response

    def save_log(self, request, response, end_time):
        user = request.user if hasattr(request, 'user') and request.user.is_authenticated else None

        # DRF 的视图在 request.resolver_match 中
        view = getattr(request.resolver_match, 'func', None)
        view_class = getattr(view, 'cls', None)

        resource = getattr(view_class, 'resource_code', 'unknown')
        
        # 推导 Action
        action_mapping = {
            'POST': 'create',
            'PUT': 'update',
            'PATCH': 'partial_update',
            'DELETE': 'delete'
        }
        # 如果视图里定义了 @action 且可以获得名字，往往存在 view.__name__，但此处采用简易推断
        action = action_mapping.get(request.method, 'unknown')
        
        # 提取操作对象 ID
        object_id = None
        if hasattr(request, 'resolver_match') and request.resolver_match and request.resolver_match.kwargs:
            object_id = request.resolver_match.kwargs.get('pk') or request.resolver_match.kwargs.get('id')
            
        # 根据权限元数据尝试获取语义化名称
        resource_name = ''
        action_name = ''
        if resource != 'unknown':
            # 后端现有的标准 code 格式，例如: rbac:user:create
            lookup_code = f"{resource}:{action}"
            try:
                # 尝试拿权限来翻译成中文名
                perm = Permission.objects.filter(code=lookup_code).first()
                if perm:
                    action_name = perm.name 
            except Exception:
                pass

        # 提取旧主数据快照
        old_data = getattr(request, '_old_data', None)

        # 过滤敏感数据
        try:
            body_bytes = getattr(request, '_cached_body', b'')
            if body_bytes:
                req_body = json.loads(body_bytes.decode('utf-8'))
                if 'password' in req_body: req_body['password'] = '******'
                req_data = req_body
            else:
                req_data = request.POST.dict() or {}
        except Exception:
            req_data = getattr(request, 'POST', {}).dict() or {}
            
        # 获取失败原因（若出现 4xx 或 5xx），成功的报文不存储
        res_data = {}
        if response.status_code >= 400:
            try:
                if hasattr(response, 'data'):
                    res_data = response.data
                else:
                    res_data = json.loads(response.content.decode('utf-8'))
            except Exception:
                # 若无法解析为 JSON，则截取前 500 个字符
                res_data = {"raw_error": response.content.decode('utf-8', errors='ignore')[:500]}
                
        # 计算总耗时
        start_time = getattr(request, 'start_time', end_time)
        duration = end_time - start_time

        # 保存到数据库
        AuditLog.objects.create(
            user=user,
            username=user.username if user else "anonymous",
            ip_address=self.get_client_ip(request),
            method=request.method,
            path=request.path,
            resource=resource,
            resource_name=resource_name,
            action=action,
            action_name=action_name,
            object_id=object_id,
            old_data=old_data,
            request_data=req_data,
            response_data=res_data,
            response_status=response.status_code,
            duration=round(duration, 3)
        )

    def get_client_ip(self, request):
        x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
        if x_forwarded_for:
            ip = x_forwarded_for.split(',')[0]
        else:
            ip = request.META.get('REMOTE_ADDR')
        return ip