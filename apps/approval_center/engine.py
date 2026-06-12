from rest_framework.response import Response
from rest_framework import status
from django.db import IntegrityError, transaction
from django.utils import timezone
from datetime import timedelta
import copy
import hashlib
from .models import ApprovalPolicy, ApprovalTicket
import json
import logging

logger = logging.getLogger(__name__)

class ProxyApprovalEngine:
    """
    统一的 Payload 代理审批中间件引擎
    简易版的审批模块，拥有审批觉得用户都可以审批
    阻塞的类型是执行的动作，当前就是流水线的执行
    负责: [拦截器触发判断]、[挂起请求保存]、[异步签发恢复]
    """

    @staticmethod
    def intercept_if_needed(request, resource_type: str, action_title: str = "高危操作", target_id: str = None, environment: str = None, extra_context: dict = None) -> tuple:
        """
        在 ViewSet 执行最初调用此方法。
        返回值: (是否被阻断挂起: bool, Response响应实例)
        """
        # 如果是通过批准的操作，直接放行！
        if getattr(request, '_is_approved_execution', False):
            return False, None

        # 获取 Payload
        request_data = request.data if hasattr(request, 'data') else request.POST
        if hasattr(request_data, 'dict'):
            raw_payload = request_data.dict()
        else:
            raw_payload = copy.deepcopy(dict(request_data))
        if extra_context:
            raw_payload.update(extra_context)

        # [优化 D] 提取 AI 确信标志
        is_ai_verified = raw_payload.get('ai_verified') is True

        # 查询系统里有没有启用的相关阻断规则
        policy_filter = {"resource_type": resource_type, "is_active": True}
        if environment:
            from django.db.models import Q
            policies = ApprovalPolicy.objects.filter(
                Q(environment=environment) | Q(environment__isnull=True) | Q(environment=''),
                **policy_filter
            )
        else:
            policies = ApprovalPolicy.objects.filter(**policy_filter)

        if not policies.exists():
            return False, None

        # [优化 C & D] 遍历筛选真正命中的策略
        matched_policy = None
        for policy in policies:
            # 1. 检查白名单：如果是AI确认且策略允许，直接跳过此策略
            if is_ai_verified and policy.auto_pass_if_ai_verified:
                continue

            # 2. 检查细粒度规则 match_rules
            match = True
            if policy.match_rules and isinstance(policy.match_rules, dict):
                for k, v in policy.match_rules.items():
                    if raw_payload.get(k) != v:
                        match = False
                        break
            
            if match:
                matched_policy = policy
                break

        # 如果没有任何一条策略最终命中，则放行
        if not matched_policy:
            return False, None

        raw_payload['_matched_policy'] = matched_policy.name

        url_path = request.get_full_path()
        fingerprint_source = {
            'submitter_id': request.user.id,
            'project_id': getattr(getattr(request, 'project', None), 'id', None),
            'resource_type': resource_type,
            'target_id': target_id,
            'method': request.method,
            'url_path': url_path,
            'payload': raw_payload,
        }
        request_fingerprint = hashlib.sha256(
            json.dumps(
                fingerprint_source,
                sort_keys=True,
                ensure_ascii=False,
                default=str,
            ).encode('utf-8')
        ).hexdigest()
        now = timezone.now()
        expires_at = now + timedelta(
            minutes=matched_policy.approval_timeout_minutes
        )

        ApprovalTicket.objects.filter(
            request_fingerprint=request_fingerprint,
            status='pending',
            expires_at__lte=now,
        ).update(
            status='canceled',
            remark='审批已超时，挂起请求已自动失效。',
            audit_time=now,
        )
        
        # 生成挂起的工单
        try:
            with transaction.atomic():
                ticket, created = ApprovalTicket.objects.get_or_create(
                    request_fingerprint=request_fingerprint,
                    status='pending',
                    defaults={
                        'title': f"申请: {action_title}",
                        'submitter': request.user,
                        'policy': matched_policy,
                        'project': getattr(request, 'project', None),
                        'resource_type': resource_type,
                        'target_id': target_id,
                        'method': request.method,
                        'url_path': url_path,
                        'payload': raw_payload,
                        'environment': environment or '',
                        'expires_at': expires_at,
                    },
                )
        except IntegrityError:
            ticket = ApprovalTicket.objects.filter(
                request_fingerprint=request_fingerprint,
                status__in=['pending', 'approved'],
            ).first()
            created = False

        if ticket is None:
            logger.error(
                "Unable to resolve active approval ticket for fingerprint %s",
                request_fingerprint,
            )
            return True, Response(
                {"detail": "审批请求正在并发处理中，请稍后重试。"},
                status=status.HTTP_409_CONFLICT,
            )
        
        # --- 触发外发告警推送 (通知拥有审批权限的人) ---
        if created:
            from apps.system_management.notifiers import notify_approval_requested
            try:
                notify_approval_requested(ticket)
            except Exception:
                pass

        # 向前端返回 202 Accepted 特殊码（表示收到了请求，但不会立即处理完它）
        res = Response({
            "code": 202,
            "message": "你的操作命中了运维安全阀！已为您自动提交审批。",
            "ticket_id": ticket.id,
            "status": "pending_approval",
            "duplicate": not created,
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
        request.project = ticket.project

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
                
                # --- [Creed's Integration] 联动更新自愈告警的 run_id ---
                try:
                    res_data = response.data if hasattr(response, 'data') else {}
                    run_id = res_data.get('run_id')
                    alert_id = ticket.payload.get('alert_id') if ticket.payload else None
                    
                    if run_id and alert_id:
                        from apps.sre_management.models import AlertEvent
                        AlertEvent.objects.filter(id=alert_id).update(latest_run_id=run_id)
                        logger.info(f"Linked AlertEvent {alert_id} to new PipelineRun {run_id} after approval.")
                except Exception as e:
                    logger.error(f"Failed to sync run_id back to alert: {str(e)}")

            ticket.execution_status_code = response.status_code
            response_data = response.data if hasattr(response, 'data') else {}
            ticket.execution_response = json.loads(
                json.dumps(response_data, ensure_ascii=False, default=str)
            )
        
        except Exception as e:
            # 捕获视图层的严重 Python 异常
            ticket.status = 'failed'
            ticket.remark = f"代理唤醒底层视图时发生致命崩溃: {str(e)}"
            ticket.execution_response = {'detail': str(e)}
            
        # 录入签批人并保存生命周期
        ticket.approver = approver_user
        ticket.audit_time = timezone.now()
        ticket.save()
        
        return ticket
