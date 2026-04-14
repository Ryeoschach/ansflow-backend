from rest_framework.response import Response
from rest_framework import status
from django.db import transaction
from django.utils import timezone
from .models import ApprovalPolicy, ApprovalTicket
import json

class ProxyApprovalEngine:
    """
    统一的 Payload 代理审批中间件引擎
    简易版的审批模块，拥有审批觉得用户都可以审批
    阻塞的类型是执行的动作，当前就是流水线的执行
    负责: [拦截器触发判断]、[挂起请求保存]、[异步签发恢复]
    """

    @staticmethod
    def intercept_if_needed(request, resource_type: str, action_title: str = "高危操作", target_id: str = None) -> tuple:
        """
        在 ViewSet 执行最初调用此方法。
        返回值: (是否被阻断挂起: bool, Response响应实例)
        """
        # 如果是通过批准的操作，直接放行！
        if getattr(request, '_is_approved_execution', False):
            return False, None

        # 查询系统里有没有启用的相关阻断规则（Todo：未来可以加强环境、角色的精确计算）
        policies = ApprovalPolicy.objects.filter(resource_type=resource_type, is_active=True)
        if not policies.exists():
            return False, None

        # 如果命中了规则，立刻执行 Payload 将当前的request做快照
        raw_payload = request.data if hasattr(request, 'data') else request.POST.dict()
        url_path = request.get_full_path()
        
        # 生成挂起的工单
        with transaction.atomic():
            ticket = ApprovalTicket.objects.create(
                title=f"申请: {action_title}",
                submitter=request.user,
                resource_type=resource_type,
                target_id=target_id,
                method=request.method,
                url_path=url_path,
                payload=raw_payload,
                status='pending'
            )
        
        # --- 触发外发告警推送 (通知拥有审批权限的人) ---
        from apps.system_management.notifiers import notify_approval_requested
        try:
            notify_approval_requested(ticket)
        except Exception:
            pass # 异步解耦，保证通知不阻塞业务核心拦截逻辑

        # 向前端返回 202 Accepted 特殊码（表示收到了请求，但不会立即处理完它）
        res = Response({
            "code": 202,
            "message": "你的操作命中了运维安全阀！已为您自动提交审批。",
            "ticket_id": ticket.id,
            "status": "pending_approval"
        }, status=status.HTTP_202_ACCEPTED)

        return True, res

    @staticmethod
    def resume_execution(ticket: ApprovalTicket, approver_user):
        """
        恢复执行被冻结的载荷（核心逻辑）。
        使用 DRF APIRequestFactory 伪造真实请求并在 Django 的路由系统直接寻址派发。
        """
        from rest_framework.test import APIRequestFactory, force_authenticate
        from django.urls import resolve

        factory = APIRequestFactory()
        method = ticket.method.upper()
        url = ticket.url_path
        payload = ticket.payload

        # 模拟发起人的各种 HTTP 方法和 Body 伪造 Request 对象
        if method == 'POST':
            request = factory.post(url, data=payload, format='json')
        elif method == 'PUT':
            request = factory.put(url, data=payload, format='json')
        elif method == 'PATCH':
            request = factory.patch(url, data=payload, format='json')
        elif method == 'DELETE':
            request = factory.delete(url, data=payload, format='json')
        else:
            raise ValueError(f"暂不支持的放行代理动作: {method}")

        # 保证底层执行记录、AuditLog 的人是真实的工单提交者，而不是审批权限的人！
        force_authenticate(request, user=ticket.submitter)

        # 打上内部通行标记，防止进入 ViewSet 里面的 intercept 再次被挂起造成死循环
        request._is_approved_execution = True

        # 路由寻址 (去掉 query 字符串去寻找 View)
        path = url.split('?')[0]
        match = resolve(path)
        
        # 跳过所有的外层中间件，直接用假 Request 调用最底层的 DRF 视图函数！
        try:
            response = match.func(request, *match.args, **match.kwargs)
            
            # DRF 的 Response 如果未经过中间件流转，必须手动 render 才能获取字节串
            if hasattr(response, 'render'):
                response.render()

            # 后处理及扫尾
            if response.status_code >= 400:
                ticket.status = 'failed'
                try:
                    error_data = response.data
                except Exception:
                    error_data = response.content.decode('utf-8', errors='ignore')
                ticket.remark = f"审批已通过，但业务底层执行失败。状态码: {response.status_code}，底座报错: {error_data}"
            else:
                ticket.status = 'finished'
                ticket.remark = "审批流转完成，操作放行成功！"
        
        except Exception as e:
            # 捕获视图层的严重 Python 异常
            ticket.status = 'failed'
            ticket.remark = f"代理唤醒底层视图时发生致命崩溃: {str(e)}"
            
        # 录入签批人并保存生命周期
        ticket.approver = approver_user
        ticket.audit_time = timezone.now()
        ticket.save()
        
        return ticket
