from django.db import models
from utils.base_model import BaseModel
from utils.fields import EncryptedTextField


class K8sCluster(BaseModel):
    AUTH_CHOICES = [
        ('kubeconfig', 'Kubeconfig 文件'),
        ('token', 'Token 认证'),
    ]

    name = models.CharField(max_length=100, unique=True, verbose_name="集群名称")
    auth_type = models.CharField(max_length=20, choices=AUTH_CHOICES, default='kubeconfig')

    # Kubeconfig 模式
    kubeconfig_content = EncryptedTextField(null=True, blank=True, verbose_name="Kubeconfig 内容")

    # Token 模式
    api_server = models.URLField(null=True, blank=True, verbose_name="API Server 地址")
    token = EncryptedTextField(null=True, blank=True, verbose_name="认证 Token")

    status = models.CharField(max_length=20, default='pending', verbose_name="连接状态")
    version = models.CharField(max_length=50, blank=True, verbose_name="K8s 版本")

    class Meta:
        db_table = 'k8s_clusters'
        verbose_name = "K8s 集群"
