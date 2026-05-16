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
    
    # 动态状态指标
    node_count = models.IntegerField(default=0, verbose_name="节点总数")
    ready_node_count = models.IntegerField(default=0, verbose_name="就绪节点数")
    cpu_capacity = models.CharField(max_length=50, blank=True, null=True, verbose_name="总CPU容量")
    memory_capacity = models.CharField(max_length=50, blank=True, null=True, verbose_name="总内存容量")
    
    last_seen = models.DateTimeField(null=True, blank=True, verbose_name="最后同步时间")
    error_message = models.TextField(null=True, blank=True, verbose_name="错误信息")

    class Meta:
        db_table = 'k8s_clusters'
        verbose_name = "K8s 集群"


class HelmRepository(BaseModel):
    name = models.CharField(max_length=100, unique=True, verbose_name="仓库名称")
    url = models.URLField(verbose_name="仓库 URL")
    
    # 认证信息
    username = models.CharField(max_length=100, null=True, blank=True, verbose_name="用户名")
    password = EncryptedTextField(null=True, blank=True, verbose_name="密码/Token")
    
    description = models.TextField(null=True, blank=True, verbose_name="备注")

    class Meta:
        db_table = 'helm_repositories'
        verbose_name = "Helm 仓库"
