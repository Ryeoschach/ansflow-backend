from django.db import models
from utils.base_model import BaseModel
from utils.fields import EncryptedCharField, EncryptedTextField
from django.db.models import JSONField

class AlertEvent(BaseModel):
    """告警事件记录"""
    SOURCE_CHOICES = (
        ('prometheus', 'Prometheus'),
        ('vmalert', 'VictoriaMetrics vmalert'),
        ('victoriametrics', 'VictoriaMetrics'),
        ('zabbix', 'Zabbix'),
        ('aliyun', 'Aliyun CloudMonitor'),
        ('other', 'Other'),
    )
    STATUS_CHOICES = (
        ('firing', '告警中'),
        ('resolved', '已恢复'),
    )
    HEALING_STATUS_CHOICES = (
        ('none', '未处理'),
        ('analyzing', 'AI 分析中'),
        ('suggested', '已有建议'),
        ('awaiting_approval', '待审批'),
        ('executing', '自愈中'),
        ('success', '自愈成功'),
        ('failed', '自愈失败'),
        ('ignored', '已忽略'),
    )

    alert_name = models.CharField(max_length=255, verbose_name="告警名称")
    severity = models.CharField(max_length=50, verbose_name="严重程度")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='firing')
    source = models.CharField(max_length=50, choices=SOURCE_CHOICES, default='prometheus')
    
    # 标签与指纹，用于去重和关联
    fingerprint = models.CharField(max_length=128, db_index=True, verbose_name="告警指纹")
    labels = JSONField(default=dict, verbose_name="告警标签")
    annotations = JSONField(default=dict, verbose_name="告警注释")
    
    # 自愈状态
    healing_status = models.CharField(max_length=20, choices=HEALING_STATUS_CHOICES, default='none')
    ai_analysis = models.TextField(blank=True, null=True, verbose_name="AI 分析结论")
    is_exported = models.BooleanField(default=False, verbose_name="是否已导出至知识库")
    suggested_pipeline = models.ForeignKey('pipeline_management.Pipeline', on_delete=models.SET_NULL, null=True, blank=True, verbose_name="建议自愈流水线")
    matched_policy_name = models.CharField(max_length=100, null=True, blank=True, verbose_name="匹配到的策略名称")
    latest_run_id = models.IntegerField(null=True, blank=True, verbose_name="最近一次运行ID")
    latest_ticket_id = models.IntegerField(null=True, blank=True, verbose_name="最近一次审批工单ID")
    trigger_type = models.CharField(max_length=20, choices=(('manual', '手动'), ('auto', '自动')), null=True, blank=True, verbose_name="触发类型")
    
    class Meta:
        db_table = 'sre_alert_event'
        ordering = ['-create_time']
        verbose_name = "告警事件"
        verbose_name_plural = verbose_name

class SelfHealingPolicy(BaseModel):
    """自愈策略配置"""
    name = models.CharField(max_length=100, verbose_name="策略名称")
    alert_match_rule = JSONField(verbose_name="告警匹配规则", help_text="例如: {'alertname': 'CPUUsageTooHigh'}")
    pipeline = models.ForeignKey('pipeline_management.Pipeline', on_delete=models.CASCADE, verbose_name="关联自愈流水线")
    project = models.ForeignKey('rbac_permission.Project', on_delete=models.SET_NULL, null=True, blank=True, related_name='self_healing_policies', verbose_name="所属项目")
    is_global = models.BooleanField(default=False, verbose_name="是否为全局兜底策略")
    is_auto_execute = models.BooleanField(default=False, verbose_name="是否自动执行", help_text="开启后无需审批自动触发流水线")
    is_active = models.BooleanField(default=True, verbose_name="是否启用")

    class Meta:
        db_table = 'sre_healing_policy'
        verbose_name = "自愈策略"
        verbose_name_plural = verbose_name


class ObservabilityDataSource(BaseModel):
    """外部观测数据源配置。"""

    TYPE_CHOICES = (
        ('victoriametrics', 'VictoriaMetrics'),
        ('victorialogs', 'VictoriaLogs'),
    )
    AUTH_CHOICES = (
        ('none', '无认证'),
        ('bearer', 'Bearer Token'),
        ('basic', 'Basic Auth'),
    )

    name = models.CharField(max_length=100, unique=True, verbose_name="数据源名称")
    type = models.CharField(max_length=30, choices=TYPE_CHOICES, verbose_name="数据源类型")
    base_url = models.URLField(max_length=500, verbose_name="访问地址")
    auth_type = models.CharField(max_length=20, choices=AUTH_CHOICES, default='none', verbose_name="认证方式")
    username = models.CharField(max_length=100, blank=True, null=True, verbose_name="用户名")
    password = EncryptedCharField(max_length=512, blank=True, null=True, verbose_name="密码")
    token = EncryptedTextField(blank=True, null=True, verbose_name="Token")
    is_default = models.BooleanField(default=False, verbose_name="是否默认")
    is_active = models.BooleanField(default=True, verbose_name="是否启用")
    timeout_seconds = models.PositiveIntegerField(default=10, verbose_name="请求超时秒数")

    class Meta:
        db_table = 'sre_observability_datasource'
        verbose_name = "观测数据源"
        verbose_name_plural = verbose_name
        ordering = ['-is_default', '-create_time']

    def __str__(self):
        return f"{self.name} ({self.type})"


class ObservedService(BaseModel):
    """项目内可诊断的服务与观测标签映射。"""

    name = models.CharField(max_length=100, verbose_name="服务名称")
    code = models.CharField(max_length=80, verbose_name="服务标识")
    project = models.ForeignKey('rbac_permission.Project', on_delete=models.CASCADE, related_name='observed_services', verbose_name="所属项目")
    environment = models.ForeignKey('host_management.Environment', on_delete=models.SET_NULL, null=True, blank=True, related_name='observed_services', verbose_name="环境")
    resource_pool = models.ForeignKey('host_management.ResourcePool', on_delete=models.SET_NULL, null=True, blank=True, related_name='observed_services', verbose_name="资源池")
    hosts = models.ManyToManyField('host_management.Host', blank=True, related_name='observed_services', verbose_name="关联主机")
    k8s_cluster = models.ForeignKey('k8s_management.K8sCluster', on_delete=models.SET_NULL, null=True, blank=True, related_name='observed_services', verbose_name="K8s 集群")
    namespace = models.CharField(max_length=120, blank=True, null=True, verbose_name="K8s 命名空间")
    metric_datasource = models.ForeignKey(ObservabilityDataSource, on_delete=models.SET_NULL, null=True, blank=True, related_name='metric_services', limit_choices_to={'type': 'victoriametrics'}, verbose_name="指标数据源")
    log_datasource = models.ForeignKey(ObservabilityDataSource, on_delete=models.SET_NULL, null=True, blank=True, related_name='log_services', limit_choices_to={'type': 'victorialogs'}, verbose_name="日志数据源")
    metric_label_selector = JSONField(default=dict, blank=True, verbose_name="指标标签选择器")
    log_label_selector = JSONField(default=dict, blank=True, verbose_name="日志标签选择器")
    metric_queries = JSONField(default=list, blank=True, verbose_name="自定义指标查询")
    log_query = models.TextField(blank=True, null=True, verbose_name="自定义日志查询")
    is_active = models.BooleanField(default=True, verbose_name="是否启用")

    class Meta:
        db_table = 'sre_observed_service'
        unique_together = ('project', 'code')
        verbose_name = "可观测服务"
        verbose_name_plural = verbose_name
        ordering = ['project__name', 'name']

    def __str__(self):
        return f"{self.project.code}/{self.code}"


class DiagnosisRun(BaseModel):
    """时间点诊断任务。"""

    STATUS_CHOICES = (
        ('pending', '待执行'),
        ('running', '执行中'),
        ('success', '成功'),
        ('failed', '失败'),
    )
    TRIGGER_CHOICES = (
        ('manual', '手动'),
        ('alert', '告警'),
        ('retry', '重试'),
    )

    title = models.CharField(max_length=200, verbose_name="诊断标题")
    project = models.ForeignKey('rbac_permission.Project', on_delete=models.SET_NULL, null=True, blank=True, related_name='diagnosis_runs', verbose_name="项目")
    service = models.ForeignKey(ObservedService, on_delete=models.SET_NULL, null=True, blank=True, related_name='diagnosis_runs', verbose_name="服务")
    alert = models.ForeignKey(AlertEvent, on_delete=models.SET_NULL, null=True, blank=True, related_name='diagnosis_runs', verbose_name="关联告警")
    trigger_type = models.CharField(max_length=20, choices=TRIGGER_CHOICES, default='manual', verbose_name="触发方式")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', verbose_name="状态")
    diagnosis_time = models.DateTimeField(verbose_name="诊断时间点")
    window_minutes = models.PositiveIntegerField(default=10, verbose_name="前后窗口分钟数")
    query_params = JSONField(default=dict, blank=True, verbose_name="查询参数")
    context_snapshot = JSONField(default=dict, blank=True, verbose_name="诊断上下文")
    ai_result = models.TextField(blank=True, null=True, verbose_name="AI 诊断结果")
    error_message = models.TextField(blank=True, null=True, verbose_name="错误信息")
    created_by = models.ForeignKey('rbac_permission.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='diagnosis_runs', verbose_name="创建人")
    started_at = models.DateTimeField(null=True, blank=True, verbose_name="开始时间")
    finished_at = models.DateTimeField(null=True, blank=True, verbose_name="结束时间")

    class Meta:
        db_table = 'sre_diagnosis_run'
        verbose_name = "时间点诊断"
        verbose_name_plural = verbose_name
        ordering = ['-create_time']

    def __str__(self):
        return self.title
