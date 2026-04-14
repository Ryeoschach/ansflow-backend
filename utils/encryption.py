import base64
import os
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.fernet import Fernet
from django.conf import settings

def get_cipher():
    """
    根据 SECRET_KEY 生成 Fernet 加密对象
    """
    # 使用 SECRET_KEY 作为 salt (Todo：实际生产中使用固定的 salt，存储在配置中)
    salt = b'ansflow_security_salt' 
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=100000,
    )
    key = base64.urlsafe_b64encode(kdf.derive(settings.SECRET_KEY.encode()))
    return Fernet(key)

def encrypt_string(text: str) -> str:
    if not text:
        return text
    cipher = get_cipher()
    return cipher.encrypt(text.encode()).decode()

def decrypt_string(encrypted_text: str) -> str:
    if not encrypted_text:
        return encrypted_text
    cipher = get_cipher()
    try:
        return cipher.decrypt(encrypted_text.encode()).decode()
    except Exception:
        # 如果解密失败（可能是明文或 key 变了），原样返回，防止系统崩溃
        return encrypted_text
