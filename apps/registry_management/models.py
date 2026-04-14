from django.db import models
from utils.base_model import BaseModel
from utils.fields import EncryptedCharField


class ImageRegistry(BaseModel):
    """
    统一镜像仓库管理
    """
    name = models.CharField(max_length=100, unique=True, verbose_name="仓库名称", help_text="如: Harbor-ProjA")
    url = models.CharField(max_length=255, verbose_name="仓库地址", help_text="如: https://hub.docker.com 或 harbor.domain.com")
    namespace = models.CharField(max_length=100, blank=True, null=True, verbose_name="默认命名空间", help_text="如: library (可选)")
    username = models.CharField(max_length=100, verbose_name="认证用户名")
    password = EncryptedCharField(max_length=255, verbose_name="认证密码/Token")
    description = models.TextField(blank=True, null=True, verbose_name="描述信息")

    class Meta:
        db_table = 'pipeline_image_registry'
        verbose_name = "镜像仓库"
        verbose_name_plural = verbose_name
        ordering = ['-create_time']

    def __str__(self):
        return self.name
