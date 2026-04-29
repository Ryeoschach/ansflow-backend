from .base import *


DEBUG = False
# 域名
ALLOWED_HOSTS = env.list('ALLOWED_HOSTS')
# 强制要求安全设置
# SECURE_SSL_REDIRECT = True
SECURE_SSL_REDIRECT = env.bool('SECURE_SSL_REDIRECT', default=False)
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True

# CSRF 信任源配置 (Django 4.0+ 生产环境必须)
CSRF_TRUSTED_ORIGINS = []
for host in ALLOWED_HOSTS:
    if host == '*':
        continue
    CSRF_TRUSTED_ORIGINS.append(f"http://{host}")
    CSRF_TRUSTED_ORIGINS.append(f"https://{host}")

# 允许跨域
CORS_ALLOW_ALL_ORIGINS = True
CORS_ALLOW_CREDENTIALS = True