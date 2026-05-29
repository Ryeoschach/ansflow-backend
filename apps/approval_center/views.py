from rest_framework import viewsets, mixins, status
from rest_framework.response import Response
from rest_framework.decorators import action
from django.utils import timezone
from drf_spectacular.utils import extend_schema, extend_schema_view, OpenApiParameter

from .models import ApprovalPolicy, ApprovalTicket, ApprovalResource
from .serializers import ApprovalPolicySerializer, ApprovalTicketSerializer, ApprovalResourceSerializer
from .engine import ProxyApprovalEngine
from .registry import approval_registry

from utils.rbac_permission import SmartRBACPermission, DataScopeMixin

@extend_schema_view(
    list=extend_schema(summary="查看审批策略列表"),
    create=extend_schema(summary="创建新的审批策略"),
    retrieve=extend_schema(summary="查看策略详情"),
    update=extend_schema(summary="修改策略内容"),
    destroy=extend_schema(summary="删除策略"),
)
class ApprovalPolicyViewSet(viewsets.ModelViewSet):
    """
    配置中心的审批阻断策略开关
    """
    queryset = ApprovalPolicy.objects.all().order_by('-create_time')
    serializer_class = ApprovalPolicySerializer
    filterset_fields = ['is_active', 'resource_type']
    permission_classes = [SmartRBACPermission]
    resource_code = 'system:approval_policy'
    permission_labels = {
        'view': {'name': '查看系统策略(拦截规则)'},
        'add': {'name': '新增阻断策略', 'danger': 'warn'},
        'edit': {'name': '修改阻断策略', 'danger': 'warn'},
        'delete': {'name': '删除阻断策略', 'danger': 'high'},
    }

from django.core.cache import cache

@extend_schema_view(
    list=extend_schema(summary="查看可拦截资源列表"),
    update=extend_schema(summary="修改资源展示信息/启用状态"),
)
class ApprovalResourceViewSet(viewsets.ModelViewSet):
    """
    可拦截资源管理（模板管理）：由系统自动发现并入库，管理员可在此进行 UI 层面的调整。
    """
    queryset = ApprovalResource.objects.all().order_by('code')
    serializer_class = ApprovalResourceSerializer
    permission_classes = [SmartRBACPermission]
    resource_code = 'system:approval_resource'
    permission_labels = {
        'view': {'name': '查看拦截资源'},
        'edit': {'name': '配置资源展示/开关'},
        'delete': {'name': '注销过时资源', 'danger': 'high'},
    }

    def get_queryset(self):
        # 如果是策略配置下拉框调用，只返回启用的
        if self.request.query_params.get('active_only') == 'true':
            return self.queryset.filter(is_active=True)
        return self.queryset

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        if instance.is_system:
            return Response({"detail": "系统内置资源不可删除，仅可禁用。"}, status=status.HTTP_400_BAD_REQUEST)
        return super().destroy(request, *args, **kwargs)

@extend_schema_view(
    list=extend_schema(summary="获取审批工单列表"),
    retrieve=extend_schema(summary="查看工单详情及 Payload 快照"),
    approve=extend_schema(summary="批准并执行拦截的任务"),
    reject=extend_schema(summary="拒绝并作废拦截的任务"),
)
class ApprovalTicketViewSet(viewsets.ReadOnlyModelViewSet):
    """
    审批总控台: 这里只允许列表查看，拦截通过/拒绝通过特有接口操作
    """
    queryset = ApprovalTicket.objects.all().select_related('submitter', 'approver').order_by('-create_time')
    serializer_class = ApprovalTicketSerializer
    filterset_fields = ['status', 'resource_type', 'submitter__username']
    
    permission_classes = [SmartRBACPermission]
    resource_code = 'system:approval_ticket'
    permission_labels = {
        'view': {'name': '查看拦截挂起清单(我的)'},
        'approve': {'name': '强制签发/放行底层指令', 'danger': 'high'},
        'reject': {'name': '一票否决/报废挂单', 'danger': 'warn'}
    }

    def get_queryset(self):
        """
        数据可见度控制矩阵：
        超级管理员 -> 看全部
        拥有 system:approval_ticket:approve 权限的角色 -> 看全部（作为全职签批人）
        普通用户 -> 只能看到 submitter=自己的挂起单
        """
        qs = super().get_queryset()
        user = self.request.user
        
        if not user or not user.is_authenticated:
            return qs.none()
            
        if user.is_superuser:
            return qs
            
        # 查询系统为用户计算出的 RBAC 功能权限环
        cache_key = f"rbac:perms:user_{user.id}"
        user_perms_list = cache.get(cache_key) or []
        user_perms = set(user_perms_list)
        
        # 如果其角色拥有这个高危权限（或者用 * 通配符），就给他升权看到全局的单子
        if 'system:approval_ticket:approve' in user_perms or '*' in user_perms:
            return qs
            
        return qs.filter(submitter=user)

    @action(detail=True, methods=['POST'])
    def approve(self, request, pk=None):
        """
        核心API：点击同意放行！支持对失败的工单进行重试。
        """
        ticket = self.get_object()
        if ticket.status not in ['pending', 'failed']:
            return Response({"detail": "该审批单当前状态不支持放行操作！"}, status=status.HTTP_400_BAD_REQUEST)
        
        # 将工单状态扭转之前，强行路由执行！！
        ProxyApprovalEngine.resume_execution(ticket, request.user)
        
        # 刷新实例拿最新状态
        ticket.refresh_from_db()

        # --- 触发审批结果通知 ---
        from apps.system_management.notifiers import notify_approval_result
        try:
            notify_approval_result(ticket)
        except Exception:
            pass

        return Response({
            "detail": "已下发同意指令！底层代理回复完成。", 
            "new_status": ticket.status, 
            "sys_remark": ticket.remark
        })


    @action(detail=True, methods=['POST'])
    def reject(self, request, pk=None):
        """
        驳回审批，永久废弃这笔被拦截的 API 请求载荷。
        """
        ticket = self.get_object()
        if ticket.status != 'pending':
            return Response({"detail": "非待办单据无法操作。"}, status=status.HTTP_400_BAD_REQUEST)
        
        remark = request.data.get('remark', '主管已否决本次高危操作！详情请线下联络。')
        
        ticket.status = 'rejected'
        ticket.approver = request.user
        ticket.audit_time = timezone.now()
        ticket.remark = remark
        ticket.save()

        # --- 🚀 触发审批结果通知 ---
        from apps.system_management.notifiers import notify_approval_result
        try:
            notify_approval_result(ticket)
        except Exception:
            pass

        return Response({"detail": "已驳回并废掉该拦截器挂起任务！"})
