from utils.base_model import BaseModel
from django.db import models


class ConfigCategory(BaseModel):
    """
    配置分类：用于对配置项进行分组
    如：redis, database, mq, logging 等
    """
    name = models.CharField(max_length=50, unique=True, verbose_name="分类标识")
    label = models.CharField(max_length=100, verbose_name="显示名称")
    description = models.TextField(blank=True, verbose_name="描述")

    class Meta:
        db_table = 'config_center_category'
        verbose_name = '配置分类'
        verbose_name_plural = '配置分类'

    def __str__(self):
        return self.label


class ConfigItem(BaseModel):
    """
    配置项：存储具体的配置键值对
    """
    VALUE_TYPE_CHOICES = [
        ('string', '字符串'),
        ('int', '整数'),
        ('float', '浮点数'),
        ('bool', '布尔值'),
        ('json', 'JSON对象'),
    ]

    category = models.ForeignKey(
        ConfigCategory,
        on_delete=models.CASCADE,
        related_name='items',
        verbose_name="所属分类"
    )
    key = models.CharField(max_length=100, verbose_name="配置键")
    value = models.JSONField(verbose_name="配置值")
    value_type = models.CharField(
        max_length=20,
        choices=VALUE_TYPE_CHOICES,
        default='string',
        verbose_name="值类型"
    )
    is_encrypted = models.BooleanField(default=False, verbose_name="是否加密")
    is_active = models.BooleanField(default=True, verbose_name="是否启用")
    description = models.CharField(max_length=200, blank=True, verbose_name="描述")

    class Meta:
        db_table = 'config_center_item'
        verbose_name = '配置项'
        verbose_name_plural = '配置项'
        unique_together = [['category', 'key']]

    def __str__(self):
        return f"{self.category.name}.{self.key}"

    def get_value(self):
        """获取配置值，如果是加密字段则解密"""
        if self.is_encrypted:
            from utils.encryption import decrypt_string
            return decrypt_string(self.value)
        return self.value

    def set_value(self, value):
        """设置配置值，如果是加密字段则加密存储"""
        if self.is_encrypted and isinstance(value, str):
            from utils.encryption import encrypt_string
            self.value = encrypt_string(value)
        else:
            self.value = value
