from django.db import models
from utils.base_model import BaseModel
from utils.fields import EncryptedCharField

class Credential(BaseModel):
    """
    系统全局凭据/密钥库 (Secret Vault)
    用于存储 SSH 私钥、数据库凭密、第三方 API Token 等敏感资产。
    """
    TYPE_CHOICES = (
        ('ssh_key', 'SSH 私钥'),
        ('login_pass', '用户名密码对'),
        ('token', 'API Token / Secret'),
        ('file', '敏感文件内容 (如证书)'),
    )

    name = models.CharField(max_length=100, unique=True, verbose_name="凭据标识", help_text="如: PROD-SSH-KEY")
    type = models.CharField(max_length=20, choices=TYPE_CHOICES, verbose_name="凭据类型")
    
    username = models.CharField(max_length=100, blank=True, null=True, verbose_name="关联用户名")
    # 虽然是 EncryptedCharField，但底层我们还会根据配置进行打码处理
    secret_value = EncryptedCharField(max_length=4096, verbose_name="加密原始值", help_text="SSH密钥、密码、Token等")
    
    description = models.TextField(blank=True, null=True, verbose_name="用途备注")
    
    class Meta:
        db_table = 'sys_credential_vault'
        verbose_name = "全局凭据"
        verbose_name_plural = verbose_name
        ordering = ['-create_time']

    def __str__(self):
        return self.name
