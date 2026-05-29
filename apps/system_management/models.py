from django.db import models
from utils.base_model import BaseModel
from django.conf import settings

class UserNotification(BaseModel):
    """用户通知消息"""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='notifications')
    title = models.CharField(max_length=255, verbose_name="标题")
    content = models.TextField(verbose_name="内容")
    is_read = models.BooleanField(default=False, verbose_name="是否已读")
    
    # 额外数据，例如 {"download_url": "/media/reports/...", "type": "report_ready"}
    extra_data = models.JSONField(default=dict, blank=True, verbose_name="额外参数")
    
    class Meta:
        db_table = 'system_user_notification'
        ordering = ['-create_time']
        verbose_name = "用户通知"
        verbose_name_plural = verbose_name
