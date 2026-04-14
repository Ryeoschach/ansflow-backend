from rest_framework import viewsets, mixins, status
from rest_framework.response import Response
from rest_framework.decorators import action
from django.utils import timezone

from .models import ApprovalPolicy, ApprovalTicket
from .serializers import ApprovalPolicySerializer, ApprovalTicketSerializer
from .engine import ProxyApprovalEngine

from utils.rbac_permission import SmartRBACPermission, DataScopeMixin

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
        核心API：点击同意放行！
        """
        ticket = self.get_object()
        if ticket.status != 'pending':
            return Response({"detail": "该审批单不在待审批状态！"}, status=status.HTTP_400_BAD_REQUEST)
        
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
