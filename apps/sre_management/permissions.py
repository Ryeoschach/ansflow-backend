import logging
from rest_framework.permissions import BasePermission
from utils.config_manager import ConfigCache

logger = logging.getLogger(__name__)


class AlertWebhookPermission(BasePermission):
    """
    Prometheus Alertmanager Webhook 鉴权拦截器。
    从配置中心获取 notification.webhook_token。
    若未配置（为空字符串或为 None），默认放行（向下兼容）。
    如果配置了，则检查请求中的 Authorization 头部或 URL Query token 参数。
    """
    def has_permission(self, request, view):
        configured_token = ConfigCache.get('notification', 'webhook_token')
        
        # 1. 未配置 Token 时向下兼容自动放行
        if not configured_token:
            return True
            
        # 2. 从 Authorization Header 提取 Token (Bearer Token 模式)
        auth_header = request.headers.get('Authorization', '')
        provided_token = None
        if auth_header.startswith('Bearer '):
            parts = auth_header.split(' ')
            if len(parts) == 2:
                provided_token = parts[1]
                
        # 3. 如果 Header 没有，尝试从 URL Query parameters 提取
        if not provided_token:
            provided_token = request.query_params.get('token')
            
        # 4. 匹配校验
        if provided_token == configured_token:
            return True
            
        logger.warning(
            f"[SRE Webhook] Auth failure. Expected token: {configured_token[:4]}..., "
            f"Provided: {provided_token[:4] if provided_token else None}... from IP: {request.META.get('REMOTE_ADDR')}"
        )
        return False
