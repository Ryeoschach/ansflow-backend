from django.db import models
from utils.base_model import BaseModel
from apps.host_management.models import ResourcePool
from django.conf import settings


class AnsibleTask(BaseModel):
    """
    Ansible 任务定义 (Job Template)
    """
    TASK_TYPE_CHOICES = (
        ('cmd', 'Ad-hoc 命令'),
        ('playbook', 'Playbook 剧本'),
    )

    name = models.CharField(max_length=128, verbose_name="任务名称")
    task_type = models.CharField(max_length=20, choices=TASK_TYPE_CHOICES, default='cmd', verbose_name="任务类型")
    
    # 关联资源池
    resource_pool = models.ForeignKey(ResourcePool, on_delete=models.SET_NULL, null=True, related_name='task_templates', verbose_name="目标资源池")
    
    # 内容详情
    content = models.TextField(verbose_name="内容", help_text="指令或剧本内容")
    extra_vars = models.JSONField(default=dict, blank=True, verbose_name="额外变量")
    
    # 超时设置 (秒)
    timeout = models.IntegerField(default=3600, verbose_name="超时时间(秒)")
    
    # 创建者
    creator = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='created_tasks', verbose_name="创建者")

    class Meta:
        db_table = 'task_ansible_template'
        verbose_name = "Ansible 任务定义"
        verbose_name_plural = verbose_name

    def __str__(self):
        return self.name


class AnsibleExecution(BaseModel):
    """
    Ansible 执行记录 (Job Instance)
    """
    STATUS_CHOICES = (
        ('pending', '排队中'),
        ('running', '运行中'),
        ('success', '成功'),
        ('failed', '失败'),
        ('unknown', '未知'),
    )

    task = models.ForeignKey(AnsibleTask, on_delete=models.CASCADE, related_name='executions', verbose_name="关联任务定义")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', verbose_name="执行状态")
    
    # 执行者
    executor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='executions', verbose_name="执行者")
    
    # 结果摘要
    result_summary = models.JSONField(null=True, blank=True, verbose_name="结果摘要")
    
    # 关联的异步任务 ID (用于停止/控制)
    celery_task_id = models.CharField(max_length=128, null=True, blank=True, verbose_name="Celery 任务 ID")

    # 时间记录
    start_time = models.DateTimeField(null=True, blank=True, verbose_name="开始时间")
    end_time = models.DateTimeField(null=True, blank=True, verbose_name="结束时间")

    # 是否来自流水线（来自流水线的 Ansible 执行不发送单独通知，由流水线统一通知）
    from_pipeline = models.BooleanField(default=False, verbose_name="来自流水线")

    class Meta:
        db_table = 'task_ansible_execution'
        verbose_name = "Ansible 执行记录"
        verbose_name_plural = verbose_name
        ordering = ['-create_time']

    def __str__(self):
        return f"{self.task.name} - {self.create_time}"


class TaskLog(models.Model):
    """
    任务执行实时日志
    """
    execution = models.ForeignKey(AnsibleExecution, on_delete=models.CASCADE, related_name='logs', verbose_name="关联执行记录", null=True, blank=True)
    host = models.CharField(max_length=128, blank=True, null=True, verbose_name="目标主机")
    output = models.TextField(verbose_name="执行输出")
    create_time = models.DateTimeField(auto_now_add=True, verbose_name="记录时间")

    class Meta:
        db_table = 'task_log'
        verbose_name = "任务日志"
        verbose_name_plural = verbose_name


class AnsibleSchedule(BaseModel):
    """
    Ansible 定时任务调度
    """
    CRON_CHOICES = (
        ('cron', '自定义 Cron'),
        ('interval', '固定间隔'),
    )
    INTERVAL_UNIT_CHOICES = (
        ('minutes', '分钟'),
        ('hours', '小时'),
        ('days', '天'),
    )

    name = models.CharField(max_length=128, verbose_name="调度名称")
    task = models.ForeignKey(AnsibleTask, on_delete=models.CASCADE, related_name='schedules', verbose_name="关联任务模板")

    # 是否启用
    is_enabled = models.BooleanField(default=True, verbose_name="是否启用")

    # 调度类型
    schedule_type = models.CharField(max_length=20, choices=CRON_CHOICES, default='custom', verbose_name="调度类型")

    # Cron 表达式（分钟 小时 天 月 周）
    cron_expression = models.CharField(max_length=64, blank=True, null=True, verbose_name="Cron 表达式",
                                        help_text="格式: 分 时 日 月 周，如 0 3 * * * 表示每天凌晨3点")

    # 间隔调度
    interval_value = models.IntegerField(default=1, verbose_name="间隔值")
    interval_unit = models.CharField(max_length=10, choices=INTERVAL_UNIT_CHOICES, default='hours', verbose_name="间隔单位")

    # Celery Beat 的 task name（关联 djcelery_solar_schedule 或自定义 periodic_task）
    periodic_task_id = models.IntegerField(null=True, blank=True, verbose_name="Celery PeriodicTask ID")

    # 下次执行时间
    next_run_time = models.DateTimeField(null=True, blank=True, verbose_name="下次执行时间")

    # 创建者
    creator = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='created_schedules', verbose_name="创建者")

    class Meta:
        db_table = 'task_ansible_schedule'
        verbose_name = "Ansible 定时调度"
        verbose_name_plural = verbose_name
        ordering = ['-create_time']

    def __str__(self):
        return f"{self.name} - {self.task.name}"
