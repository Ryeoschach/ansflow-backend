from django.db import models
from utils.base_model import BaseModel
from django.db.models import JSONField

class AlertEvent(BaseModel):
    """告警事件记录"""
    SOURCE_CHOICES = (
        ('prometheus', 'Prometheus'),
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
