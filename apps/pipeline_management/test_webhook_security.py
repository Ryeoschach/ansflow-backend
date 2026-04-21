import time
from unittest.mock import patch
from django.test import TestCase
from utils.webhook_security import (
    compute_signature,
    verify_webhook_signature,
    MAX_TIMESTAMP_OFFSET,
)


class ComputeSignatureTest(TestCase):
    """测试签名计算"""

    def test_compute_signature_basic(self):
        """基本签名计算"""
        sig = compute_signature("mysecret", "1234567890", b'{"event":"push"}')
        self.assertTrue(sig.startswith("sha256="))
        self.assertEqual(len(sig), 7 + 64)  # "sha256=" + 64 hex chars

    def test_empty_secret_returns_empty(self):
        """空密钥返回空字符串"""
        self.assertEqual(compute_signature("", "1234567890", b'body'), "")

    def test_deterministic(self):
        """相同输入产生相同签名"""
        sig1 = compute_signature("secret", "1234567890", b'body')
        sig2 = compute_signature("secret", "1234567890", b'body')
        self.assertEqual(sig1, sig2)

    def test_different_body_different_sig(self):
        """不同 body 产生不同签名"""
        sig1 = compute_signature("secret", "1234567890", b'body1')
        sig2 = compute_signature("secret", "1234567890", b'body2')
        self.assertNotEqual(sig1, sig2)


class VerifyWebhookSignatureTest(TestCase):
    """测试签名验证"""

    def _make_headers(self, secret, body):
        """生成合法的签名 header"""
        timestamp = str(int(time.time()))
        sig = compute_signature(secret, timestamp, body)
        return sig, timestamp  # compute_signature 已包含 "sha256=" 前缀

    def test_valid_signature(self):
        """合法签名通过验证"""
        secret = "mysecret"
        body = b'{"event":"push"}'
        sig_header, ts_header = self._make_headers(secret, body)

        is_valid, err = verify_webhook_signature(secret, sig_header, ts_header, body)
        self.assertTrue(is_valid)
        self.assertEqual(err, "")

    def test_valid_signature_with_query_params(self):
        """带查询参数的签名验证（body 可能为空）"""
        secret = "mysecret"
        body = b''
        sig_header, ts_header = self._make_headers(secret, body)

        is_valid, err = verify_webhook_signature(secret, sig_header, ts_header, body)
        self.assertTrue(is_valid)

    def test_missing_timestamp(self):
        """缺少时间戳"""
        is_valid, err = verify_webhook_signature("secret", "sha256=abc", None, b'body')
        self.assertFalse(is_valid)
        self.assertEqual(err, "missing_timestamp")

    def test_missing_signature(self):
        """缺少签名"""
        is_valid, err = verify_webhook_signature("secret", None, "1234567890", b'body')
        self.assertFalse(is_valid)
        self.assertEqual(err, "missing_signature")

    def test_invalid_timestamp_format(self):
        """时间戳格式错误"""
        is_valid, err = verify_webhook_signature("secret", "sha256=abc", "not-a-number", b'body')
        self.assertFalse(is_valid)
        self.assertEqual(err, "invalid_timestamp")

    def test_timestamp_too_old(self):
        """时间戳过旧（replay 攻击）"""
        old_timestamp = str(int(time.time()) - MAX_TIMESTAMP_OFFSET - 10)
        body = b'body'
        sig = compute_signature("secret", old_timestamp, body)

        is_valid, err = verify_webhook_signature("secret", sig, old_timestamp, body)
        self.assertFalse(is_valid)
        self.assertEqual(err, "timestamp_too_old")

    def test_timestamp_too_future(self):
        """时间戳未来（时钟偏移）"""
        future_timestamp = str(int(time.time()) + MAX_TIMESTAMP_OFFSET + 10)
        body = b'body'
        sig = compute_signature("secret", future_timestamp, body)

        is_valid, err = verify_webhook_signature("secret", sig, future_timestamp, body)
        self.assertFalse(is_valid)
        self.assertEqual(err, "timestamp_too_old")

    def test_wrong_signature(self):
        """签名被篡改"""
        timestamp = str(int(time.time()))
        body = b'body'
        sig_header = "sha256=0000000000000000000000000000000000000000000000000000000000000000"

        is_valid, err = verify_webhook_signature("secret", sig_header, timestamp, body)
        self.assertFalse(is_valid)
        self.assertEqual(err, "signature_mismatch")

    def test_tampered_body(self):
        """body 被篡改"""
        secret = "secret"
        timestamp = str(int(time.time()))
        body = b'original'
        sig_header, _ = self._make_headers(secret, body)

        # 用修改后的 body 验证
        is_valid, err = verify_webhook_signature(secret, sig_header, timestamp, b'tampered')
        self.assertFalse(is_valid)
        self.assertEqual(err, "signature_mismatch")

    def test_empty_secret_skips_verification(self):
        """未配置密钥时跳过验证（兼容旧行为）"""
        is_valid, err = verify_webhook_signature("", None, None, b'body')
        self.assertTrue(is_valid)
        self.assertEqual(err, "")

    def test_within_timestamp_tolerance(self):
        """在允许范围内的时间戳"""
        # 时间戳刚超过 4 分钟（还在 5 分钟窗口内）
        near_old_timestamp = str(int(time.time()) - MAX_TIMESTAMP_OFFSET + 30)
        body = b'body'
        sig = compute_signature("secret", near_old_timestamp, body)

        is_valid, err = verify_webhook_signature("secret", sig, near_old_timestamp, body)
        self.assertTrue(is_valid)

    def test_constant_time_comparison(self):
        """使用常数时间比较（防时序攻击）——结果正确即可"""
        timestamp = str(int(time.time()))
        body = b'body'
        # 正确签名
        correct_sig = compute_signature("secret", timestamp, body)
        # 错误签名（只有最后一位不同）
        wrong_sig = correct_sig[:-1] + ("0" if correct_sig[-1] != "0" else "1")

        is_valid, err = verify_webhook_signature("secret", correct_sig, timestamp, body)
        self.assertTrue(is_valid)

        is_valid, err = verify_webhook_signature("secret", wrong_sig, timestamp, body)
        self.assertFalse(is_valid)
