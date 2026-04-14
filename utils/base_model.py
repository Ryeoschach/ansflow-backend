from django.db import models
class BaseModel(models.Model):
    """
    抽象基类：为所有模型提供创建时间和更新时间
    """
    create_time = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    update_time = models.DateTimeField(auto_now=True, verbose_name="更新时间")
    remark = models.TextField(blank=True, null=True, verbose_name="备注")

    class Meta:
        # abstract = True，Django 不会为这个模型创建数据库表
        abstract = True
        ordering = ['-create_time']