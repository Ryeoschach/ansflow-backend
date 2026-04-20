"""
Django 信号定义
"""
from django.dispatch import Signal

# 配置变更信号
# 发送者会收到 category 和 key 参数
config_changed = Signal()
