from .base import *


DEBUG = False
# 域名
ALLOWED_HOSTS = env.list('ALLOWED_HOSTS')
# 强制要求安全设置
# SECURE_SSL_REDIRECT = True
SECURE_SSL_REDIRECT = env.bool('SECURE_SSL_REDIRECT', default=False)
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True