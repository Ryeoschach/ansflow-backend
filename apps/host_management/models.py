from django.db import models
from utils.base_model import BaseModel
from utils.fields import EncryptedCharField, EncryptedTextField

# Create your models here.
class SshCredential(BaseModel):
    """
    SSH 登录凭据：存储用户名、密码或私钥
    将密码/密钥加密处理
    """
    AUTH_TYPES = (
        ('password', '账号密码'),
        ('key', 'SSH 密钥'),
    )

    name = models.CharField(max_length=100, unique=True, verbose_name="凭据名称")
    username = models.CharField(max_length=100, default='root', verbose_name="用户名")
    auth_type = models.CharField(max_length=20, choices=AUTH_TYPES, default='password', verbose_name="认证方式")
    password = EncryptedCharField(max_length=512, blank=True, null=True, verbose_name="密码")
    private_key = EncryptedTextField(blank=True, null=True, verbose_name="私钥内容")
    passphrase = EncryptedCharField(max_length=512, blank=True, null=True, verbose_name="私钥密码")

    class Meta:
        db_table = 'cmdb_ssh_credential'
        verbose_name = "SSH 凭据"
        verbose_name_plural = verbose_name

    def __str__(self):
        return f"{self.name} ({self.username})"


class Environment(BaseModel):
    """
    业务逻辑环境：开发(dev)、测试(test)、预发(uat)、生产(prod)
    """
    name = models.CharField(max_length=50, unique=True, verbose_name="环境名称")
    code = models.CharField(max_length=20, unique=True, verbose_name="环境标识")

    class Meta:
        db_table = 'cmdb_environment'
        verbose_name = "环境管理"
        verbose_name_plural = verbose_name

    def __str__(self):
        return f"{self.name} ({self.code})"


class Platform(BaseModel):
    """
    基础架构平台/云厂商：如 阿里云、腾讯云、本地虚拟化、物理机房、自建K8s集群等
    """
    PLATFORM_TYPES = (
        ('aliyun', '阿里云 (Aliyun)'),
        ('tencent', '腾讯云 (Tencent)'),
        ('aws', '亚马逊云 (AWS)'),
        ('vmware', '虚拟化 (VMware)'),
        ('k8s', '容器集群 (Kubernetes)'),
        ('physical', '传统机房 (Physical)'),
        ('other', '其他 (Other)'),
    )

    name = models.CharField(max_length=100, null=True, blank=True, verbose_name="平台名称")
    type = models.CharField(max_length=50, choices=PLATFORM_TYPES, default='vmware', null=True, blank=True, verbose_name="平台类型")

    # 云平台连接信息 (AccessKey/SecretKey/API Endpoint)
    access_key = models.CharField(max_length=255, blank=True, null=True, verbose_name="Access Key")
    secret_key = models.CharField(max_length=255, blank=True, null=True, verbose_name="Secret Key")
    api_endpoint = models.CharField(max_length=255, blank=True, null=True, verbose_name="API 端点")

    # 连通性状态
    CONNECTIVITY_CHOICES = (
        (0, '未验证'),
        (1, '正常'),
        (2, '异常'),
    )
    connectivity_status = models.IntegerField(choices=CONNECTIVITY_CHOICES, default=0, verbose_name="连通性状态")
    last_verified_at = models.DateTimeField(null=True, blank=True, verbose_name="上次验证时间")
    error_message = models.TextField(null=True, blank=True, verbose_name="错误信息")

    status = models.BooleanField(default=True, verbose_name="启用状态")

    # 平台默认登录凭据
    default_credential = models.ForeignKey(
        SshCredential,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='platforms',
        verbose_name="平台默认 SSH 凭据"
    )

    class Meta:
        db_table = 'cmdb_platform'
        verbose_name = "云平台配置"
        verbose_name_plural = verbose_name

    def __str__(self):
        return f"{self.name} ({self.get_type_display()})"


class Host(BaseModel):
    """
    服务器/主机信息
    """
    STATUS_CHOICES = (
        (0, '下线'),
        (1, '在线'),
        (2, '故障'),
        (3, '备用'),
    )

    # 关联环境
    env = models.ForeignKey(
        Environment,
        on_delete=models.PROTECT,  # 防止误删环境导致主机数据孤立
        related_name='hosts',
        verbose_name="所属环境"
    )

    # 运行在哪个底层平台上
    platform = models.ForeignKey(
        Platform,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name='hosts',
        verbose_name="所属平台"
    )

    hostname = models.CharField(max_length=128, unique=True, verbose_name="主机名")
    ports = models.CharField(max_length=128, verbose_name="开放端口", blank=True, null=True)
    ip_address = models.GenericIPAddressField(verbose_name="外网IP", blank=True, null=True)
    private_ip = models.GenericIPAddressField(verbose_name="内网IP", blank=True, null=True)

    # 硬件/操作系统信息
    os_type = models.CharField(max_length=64, default='Linux', verbose_name="操作系统")
    cpu = models.IntegerField(verbose_name="CPU核数", default=2)
    memory = models.IntegerField(verbose_name="内存(GB)", default=2)
    disk = models.IntegerField(verbose_name="磁盘(GB)", default=20)

    # 状态与资源码
    status = models.IntegerField(choices=STATUS_CHOICES, default=1, verbose_name="主机状态")

    # 主机特定登录凭据 (覆盖平台设置)
    credential = models.ForeignKey(
        SshCredential,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='hosts',
        verbose_name="SSH 登录凭据"
    )

    class Meta:
        db_table = 'cmdb_host'
        verbose_name = "主机管理"
        verbose_name_plural = verbose_name
        ordering = ['-create_time']

    def __str__(self):
        return f"{self.hostname} - {self.private_ip or self.ip_address}"


class ResourcePool(BaseModel):
    """
    用于 Ansible 执行或项目权限隔离的业务侧集合。
    一个资源池可以精选组合来自不同“平台”和“环境”的主机。
    """
    name = models.CharField(max_length=100, unique=True, verbose_name="资源池名称")
    code = models.CharField(max_length=50, unique=True, verbose_name="资源池标识(用作Ansible中Group名称)",
                            help_text="只能包含英文和下划线，如: web_servers")

    # 组合关联，一个池子包含任意多台主机
    hosts = models.ManyToManyField(
        Host,
        blank=True,
        related_name='pools',
        verbose_name="包含的主机"
    )

    # Todo: 可以添加一个 owner 字段指向 User，表示这个池子的负责人

    class Meta:
        db_table = 'cmdb_resource_pool'
        verbose_name = "资源池"
        verbose_name_plural = verbose_name

    def __str__(self):
        return f"{self.name} (主机数: {self.hosts.count()})"