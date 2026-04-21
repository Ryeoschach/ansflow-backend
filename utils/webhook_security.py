"""
Webhook 签名验证工具
实现 GitHub 风格的 HMAC-SHA256 签名验证，防伪造 + 防 replay 攻击
"""
import hmac
import hashlib
import time
import logging
from typing import Tuple

logger = logging.getLogger(__name__)

# 允许的最大时间戳偏移（秒），防止 replay 攻击
MAX_TIMESTAMP_OFFSET = 300  # 5 分钟


def compute_signature(secret_key: str, timestamp: str, body: bytes) -> str:
    """
    计算 HMAC-SHA256 签名
    与 GitHub 格式兼容: sha256=<hex_digest>
    """
    if not secret_key:
        return ""
    expected = hmac.new(
        secret_key.encode('utf-8'),
        f"{timestamp}.".encode('utf-8') + body,
        hashlib.sha256
    ).hexdigest()
    return f"sha256={expected}"


def verify_webhook_signature(
    secret_key: str,
    signature_header: str | None,
    timestamp_header: str | None,
    body: bytes,
) -> Tuple[bool, str]:
    """
    验证 Webhook 请求签名

    Args:
        secret_key:         Webhook 配置的密钥
        signature_header:   X-AnsFlow-Signature header 值
        timestamp_header:    X-AnsFlow-Timestamp header 值（Unix 时间戳字符串）
        body:               原始请求体（bytes）

    Returns:
        (is_valid, error_message)
        - (True, "")                     验证通过
        - (False, "missing_timestamp")  缺少时间戳
        - (False, "invalid_timestamp")  时间戳格式错误
        - (False, "timestamp_too_old") 时间戳超过允许范围
        - (False, "missing_signature")  缺少签名
        - (False, "signature_mismatch") 签名不匹配
    """
    if not secret_key:
        # 没有配置密钥时，跳过验证（兼容旧行为）
        return True, ""

    if not timestamp_header:
        return False, "missing_timestamp"
    if not signature_header:
        return False, "missing_signature"

    # 1. 验证时间戳（防 replay）
    try:
        timestamp_int = int(timestamp_header)
    except (ValueError, TypeError):
        return False, "invalid_timestamp"

    current_time = int(time.time())
    if abs(current_time - timestamp_int) > MAX_TIMESTAMP_OFFSET:
        return False, "timestamp_too_old"

    # 2. 计算期望签名
    expected_sig = compute_signature(secret_key, timestamp_header, body)

    # 3. 常数时间比较（防时序攻击）
    if not hmac.compare_digest(expected_sig, signature_header):
        return False, "signature_mismatch"

    return True, ""
