import logging
from rest_framework.views import exception_handler
from rest_framework.response import Response
from rest_framework import status

logger = logging.getLogger(__name__)


def custom_exception_handler(exc, context):
    """
    自定义异常处理：将 DRF 原生的错误内容格式化为统一的 JSON
    """
    # 调用 DRF 原生的捕获逻辑
    response = exception_handler(exc, context)

    if response is not None:
        # 已知的 DRF 异常（如 403, 401, 400 等）
        msg = response.data
        if isinstance(msg, dict):
            # 将复杂的字典错误信息简化（例如：{"username": ["必填"]} -> "username: 必填"）
            msg = "; ".join([f"{k}: {v[0] if isinstance(v, list) else v}" for k, v in msg.items()])

        response.data = {
            'code': response.status_code,
            'message': msg,
            'data': None
        }
    else:
        logger.error(f"Internal Server Error: {exc}", exc_info=True)
        response = Response({
            'code': 500,
            'message': f"服务器内部错误: {str(exc)}",
            'data': None
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    return response
