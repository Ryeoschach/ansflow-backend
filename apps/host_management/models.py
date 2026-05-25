from django.db import models
from django.core.validators import RegexValidator
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
    description = models.TextField(blank=True, null=True, verbose_name="描述备注")

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
    color = models.CharField(max_length=20, default='#1890ff', verbose_name="环境颜色")

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

    # 云平台连接信息 (AccessKey/SecretKey/API Endpoint) - 已改为加密存储
    access_key = EncryptedCharField(max_length=255, blank=True, null=True, verbose_name="Access Key")
    secret_key = EncryptedCharField(max_length=255, blank=True, null=True, verbose_name="Secret Key")
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
    code = models.CharField(
        max_length=50, 
        unique=True, 
        verbose_name="资源池标识(用作Ansible中Group名称)",
        help_text="只能包含英文数字和下划线，且必须以字母开头，如: web_servers",
        validators=[
            RegexValidator(
                regex=r'^[a-zA-Z][a-zA-Z0-9_]*$',
                message="资源池标识必须以字母开头，且仅能包含字母、数字和下划线。"
            )
        ]
    )
    remark = models.TextField(blank=True, null=True, verbose_name="备注")

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


class HostBaseline(BaseModel):
    """
    主机基线配置：定义资源池的“期望状态”
    """
    name = models.CharField(max_length=100, verbose_name="基线名称")
    resource_pool = models.ForeignKey(ResourcePool, on_delete=models.CASCADE, related_name='baselines', verbose_name="目标资源池")

    # 期望的 Ansible Playbook (用于检查状态)
    check_playbook = models.TextField(verbose_name="检查剧本 (Ansible)", help_text="用于巡检主机状态的 Playbook 内容")

    # 自动修复配置
    auto_remediate = models.BooleanField(default=False, verbose_name="发现异常自动修复")
    remediate_playbook = models.TextField(blank=True, null=True, verbose_name="修复剧本", help_text="当基线不通过时自动运行的 Playbook")

    is_active = models.BooleanField(default=True, verbose_name="是否启用")
    last_check_time = models.DateTimeField(null=True, blank=True, verbose_name="最近巡检时间")
    last_check_status = models.CharField(max_length=20, default='pending', verbose_name="最近巡检状态")
    last_execution_id = models.IntegerField(null=True, blank=True, verbose_name="最近执行ID")

    class Meta:

        db_table = 'cmdb_host_baseline'
        verbose_name = "主机基线"
        verbose_name_plural = verbose_name

    def __str__(self):
        return f"{self.name} -> {self.resource_pool.name}"


class ComplianceFramework(BaseModel):
    """
    合规框架模型：如“网络安全等级保护 2.0 (等保2.0)”
    """
    name = models.CharField(max_length=100, verbose_name="框架名称")
    code = models.CharField(max_length=50, unique=True, verbose_name="框架标识")
    version = models.CharField(max_length=50, blank=True, null=True, verbose_name="版本号")
    description = models.TextField(blank=True, null=True, verbose_name="框架描述")

    class Meta:
        db_table = 'cmdb_compliance_framework'
        verbose_name = "合规框架"
        verbose_name_plural = verbose_name

    def __str__(self):
        return f"{self.name} ({self.version or 'v1.0'})"


class ComplianceClause(BaseModel):
    """
    合规条款模型：树状层级结构，如“身份鉴别 -> 密码复杂度要求”
    """
    framework = models.ForeignKey(ComplianceFramework, on_delete=models.CASCADE, related_name='clauses', verbose_name="所属框架")
    parent = models.ForeignKey('self', on_delete=models.CASCADE, null=True, blank=True, related_name='children', verbose_name="上级条款")
    code = models.CharField(max_length=50, verbose_name="条款编号")
    name = models.CharField(max_length=100, verbose_name="条款名称")
    description = models.TextField(blank=True, null=True, verbose_name="条款描述")
    sort_order = models.IntegerField(default=0, verbose_name="排序序号")

    class Meta:
        db_table = 'cmdb_compliance_clause'
        ordering = ['sort_order', 'id']
        verbose_name = "合规条款"
        verbose_name_plural = verbose_name

    def __str__(self):
        return f"[{self.code}] {self.name}"

    @property
    def compliance_status(self):
        """
        动态计算条款的合规状态：
        - 如果有子条款，返回所有子条款的逻辑状态（只要有一个 failed 则为 failed，全部 success 为 success，其余为 pending）
        - 如果为叶子条款，根据关联的主机基线状态返回（只要有一个 failed 则为 failed，全部 success 为 success，正在运行为 running，其余为 pending）
        """
        children = self.children.all()
        if children.exists():
            statuses = [c.compliance_status for c in children]
            if 'failed' in statuses:
                return 'failed'
            if all(s == 'success' for s in statuses):
                return 'success'
            return 'pending'

        mappings = self.baseline_mappings.all()
        if not mappings.exists():
            return 'pending'

        statuses = [m.baseline.last_check_status for m in mappings]
        if 'failed' in statuses:
            return 'failed'
        if 'running' in statuses:
            return 'running'
        if all(s == 'success' for s in statuses):
            return 'success'
        return 'pending'


class ComplianceBaselineMapping(BaseModel):
    """
    合规条款与主机基线的映射关系表 (ManyToMany)
    """
    clause = models.ForeignKey(ComplianceClause, on_delete=models.CASCADE, related_name='baseline_mappings', verbose_name="合规条款")
    baseline = models.ForeignKey(HostBaseline, on_delete=models.CASCADE, related_name='compliance_mappings', verbose_name="主机基线")

    class Meta:
        db_table = 'cmdb_compliance_baseline_mapping'
        unique_together = ('clause', 'baseline')
        verbose_name = "条款基线映射"
        verbose_name_plural = verbose_name

    def __str__(self):
        return f"{self.clause.code} <-> {self.baseline.name}"