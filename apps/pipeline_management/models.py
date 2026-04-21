from django.db import models
from utils.base_model import BaseModel
from django.db.models import JSONField

class Pipeline(BaseModel):
    """流水线模板：基于 ReactFlow 生成的 DAG"""
    name = models.CharField(max_length=100, unique=True, verbose_name="流水线名称")
    desc = models.TextField(blank=True, null=True, verbose_name="描述")
    graph_data = JSONField(default=dict, verbose_name="前端流程图数据") # 保存 ReactFlow 导出的整个 JSON
    creator = models.ForeignKey('rbac_permission.User', on_delete=models.SET_NULL, null=True, verbose_name="创建人")
    is_active = models.BooleanField(default=True, verbose_name="是否启用")
    timeout = models.IntegerField(default=3600, verbose_name="流水线全局超时时间(秒)")

    # Cron 调度相关字段
    cron_expression = models.CharField(max_length=100, blank=True, null=True, verbose_name="Cron调度表达式", help_text="例如: 0 2 * * *")
    is_cron_enabled = models.BooleanField(default=False, verbose_name="是否启用定时调度")
    celery_periodic_task_id = models.IntegerField(null=True, blank=True, verbose_name="绑定的 Celery PeriodicTask 的 ID")

    class Meta:
        db_table = 'pipeline_template'
        ordering = ['-create_time']

    def save(self, *args, **kwargs):
        # 0. 判断是否为更新操作（已有 ID）
        is_update = self.pk is not None

        # 1. 拦截保存动作，先把对象存到数据库从而产生 ID
        super(Pipeline, self).save(*args, **kwargs)

        # 2. 保存时自动创建版本快照（仅当有 graph_data 且为更新时）
        if is_update and self.graph_data:
            self._create_version_snapshot()

        # 3. 如果成功安装了 django_celery_beat，则注册/更新它的定期任务
        try:
            from django_celery_beat.models import PeriodicTask, CrontabSchedule
            import json

            # 统一清理表达式字符串
            cron_str = (self.cron_expression or "").strip()

            # 只要开启且有值，就去维护具体的 PeriodicTask
            if self.is_cron_enabled and cron_str:
                parts = cron_str.split()
                if len(parts) == 5:
                    schedule, _ = CrontabSchedule.objects.get_or_create(
                        minute=parts[0],
                        hour=parts[1],
                        day_of_month=parts[2],
                        month_of_year=parts[3],
                        day_of_week=parts[4],
                        timezone='Asia/Shanghai'
                    )

                    task = None
                    if self.celery_periodic_task_id:
                        task = PeriodicTask.objects.filter(id=self.celery_periodic_task_id).first()

                    # 容灾：如果 ID 丢了，尝试通过名称找回（解决手动删改库导致的断连）
                    task_name = f'Pipeline_Cron_{self.id}_Trigger'
                    if not task:
                        task = PeriodicTask.objects.filter(name=task_name).first()

                    if task:
                        # 更新已有
                        task.crontab = schedule
                        task.enabled = True
                        task.save()
                        # 更新 ID 绑定（如果之前是丢的）
                        if not self.celery_periodic_task_id:
                            Pipeline.objects.filter(id=self.id).update(celery_periodic_task_id=task.id)
                    else:
                        # 新建绑定
                        task = PeriodicTask.objects.create(
                            name=task_name,
                            crontab=schedule,
                            task='apps.pipeline_management.tasks.execute_pipeline_cron',
                            args=json.dumps([self.id]),
                            enabled=True
                        )
                        Pipeline.objects.filter(id=self.id).update(celery_periodic_task_id=task.id)
            else:
                # 情况 A：关闭开关但保留表达式 -> 挂起 (Suspend)
                # 情况 B：清空表达式 -> 销毁 (Destroy)
                if self.celery_periodic_task_id:
                    task = PeriodicTask.objects.filter(id=self.celery_periodic_task_id).first()
                    if task:
                        if not cron_str:
                            # 表达式都没了，老任务该寿终正寝了
                            task.delete()
                            Pipeline.objects.filter(id=self.id).update(celery_periodic_task_id=None)
                        else:
                            # 只是关了开关，任务保留但禁用
                            task.enabled = False
                            task.save()
        except ImportError:
            pass

    def _create_version_snapshot(self):
        """自动创建版本快照"""
        from django.utils import timezone as tz
        last_version = self.versions.order_by('-version_number').first()
        next_version = (last_version.version_number + 1) if last_version else 1

        # 取消之前 current 版本标记
        self.versions.filter(is_current=True).update(is_current=False)

        PipelineVersion.objects.create(
            pipeline=self,
            version_number=next_version,
            graph_data=self.graph_data,
            name=self.name,
            desc=self.desc,
            creator=self.creator,
            change_summary=f'自动快照 v{next_version}',
            is_current=True,
        ) 


class PipelineRun(BaseModel):
    """单次流水线运行实例"""
    STATUS_CHOICES = (
        ('pending', '等待执行'),
        ('running', '运行中'),
        ('success', '执行成功'),
        ('failed', '执行失败'),
        ('cancelled', '已取消'),
    )
    TRIGGER_TYPE_CHOICES = (
        ('manual', '手动触发'),
        ('schedule', '定时触发'),
        ('webhook', 'Webhook 触发'),
        ('retry', '重试触发'),
    )
    pipeline = models.ForeignKey(Pipeline, on_delete=models.CASCADE, related_name='runs')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    trigger_user = models.ForeignKey('rbac_permission.User', on_delete=models.SET_NULL, null=True, blank=True)
    celery_task_id = models.CharField(max_length=128, null=True, blank=True, verbose_name="DAG 任务 ID")
    start_time = models.DateTimeField(null=True, blank=True)
    end_time = models.DateTimeField(null=True, blank=True)
    trigger_type = models.CharField(max_length=20, choices=TRIGGER_TYPE_CHOICES, default='manual', verbose_name="触发类型")
    parent_run = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='retry_runs', verbose_name="父级 Run（重试时）")
    start_node_id = models.CharField(max_length=128, null=True, blank=True, verbose_name="重试起始节点 ID")

    class Meta:
        db_table = 'pipeline_run_instance'
        ordering = ['-create_time']

class PipelineNodeRun(BaseModel):
    """流水线运行时：具体各个节点的执行状态与日志"""
    STATUS_CHOICES = (
        ('pending', '等待前置节点完成'),
        ('running', '正在执行本节点'),
        ('success', '本节点成功'),
        ('failed', '本节点失败'),
        ('skipped', '已跳过'),
        ('cancelled', '已取消'),
    )
    run = models.ForeignKey(PipelineRun, on_delete=models.CASCADE, related_name='nodes')
    node_id = models.CharField(max_length=50, verbose_name="前端画布上的节点ID") # 例如 'dndnode_1'
    node_type = models.CharField(max_length=50, verbose_name="节点类型") # config.type, 例如 'ansible', 'k8s_deploy'
    node_label = models.CharField(max_length=100, blank=True, verbose_name="节点可视化名称")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    
    start_time = models.DateTimeField(null=True, blank=True)
    end_time = models.DateTimeField(null=True, blank=True)
    logs = models.TextField(blank=True, null=True, verbose_name="执行日志")
    output_data = JSONField(default=dict, blank=True, null=True, verbose_name="节点产出变量") # 供下游节点引用
    celery_task_id = models.CharField(max_length=128, null=True, blank=True, verbose_name="单个节点的任务 ID")
    retry_count = models.IntegerField(default=0, verbose_name="已重试次数")

    class Meta:
        db_table = 'pipeline_node_run_log'
        unique_together = ('run', 'node_id') # 同一次执行里，一个 Node 最多一条记录
        ordering = ['create_time']

class CIEnvironment(BaseModel):
    """统一管理流水线的底层执行沙箱构建环境"""
    name = models.CharField(max_length=100, unique=True, verbose_name="环境展示名称")
    image = models.CharField(max_length=255, verbose_name="Docker 镜像地址")
    type = models.CharField(max_length=50, verbose_name="技术栈标签", blank=True, null=True)
    description = models.TextField(blank=True, null=True, verbose_name="用途描述")
    status = models.CharField(max_length=20, default='READY', verbose_name="状态") # PULLING, READY, ERROR

    class Meta:
        db_table = 'pipeline_ci_environment'
        ordering = ['-create_time']


class PipelineVersion(BaseModel):
    """
    流水线模板版本快照：每次保存流水线时自动生成版本记录
    """
    pipeline = models.ForeignKey(Pipeline, on_delete=models.CASCADE, related_name='versions', verbose_name="所属流水线")
    version_number = models.IntegerField(verbose_name="版本号")
    graph_data = JSONField(default=dict, verbose_name="版本快照数据")
    name = models.CharField(max_length=100, verbose_name="版本名称（快照时）")
    desc = models.TextField(blank=True, null=True, verbose_name="版本描述（快照时）")
    creator = models.ForeignKey('rbac_permission.User', on_delete=models.SET_NULL, null=True, verbose_name="操作人")
    change_summary = models.CharField(max_length=255, blank=True, null=True, verbose_name="变更说明")
    is_current = models.BooleanField(default=False, verbose_name="是否为当前版本")

    class Meta:
        db_table = 'pipeline_version'
        verbose_name = "流水线版本"
        verbose_name_plural = verbose_name
        ordering = ['-version_number']
        unique_together = ('pipeline', 'version_number')
        indexes = [
            models.Index(fields=['pipeline', '-version_number']),
        ]

    def __str__(self):
        return f"{self.pipeline.name} v{self.version_number}"


class PipelineWebhook(BaseModel):
    """
    流水线 Webhook 触发配置
    """
    EVENT_TYPE_CHOICES = [
        ('push', '代码推送'),
        ('tag', '标签创建'),
        ('pull_request', 'Pull Request'),
        ('manual', '手动触发'),
    ]

    pipeline = models.ForeignKey(Pipeline, on_delete=models.CASCADE, related_name='webhooks', verbose_name="关联流水线")
    name = models.CharField(max_length=100, verbose_name="Webhook 名称", help_text="如: GitHub Push Trigger")
    event_type = models.CharField(max_length=20, choices=EVENT_TYPE_CHOICES, default='push', verbose_name="触发事件")
    repository_url = models.CharField(max_length=512, blank=True, null=True, verbose_name="仓库地址", help_text="如: https://github.com/org/repo")
    branch_filter = models.CharField(max_length=255, blank=True, null=True, verbose_name="分支过滤", help_text="留空表示所有分支，支持 glob 匹配如: main, release/*")
    secret_key = models.CharField(max_length=128, blank=True, null=True, verbose_name="签名密钥", help_text="用于验证请求来源")
    is_active = models.BooleanField(default=True, verbose_name="是否启用")
    description = models.TextField(blank=True, null=True, verbose_name="描述")
    last_trigger_time = models.DateTimeField(null=True, blank=True, verbose_name="最近触发时间")
    trigger_count = models.IntegerField(default=0, verbose_name="累计触发次数")

    class Meta:
        db_table = 'pipeline_webhook'
        verbose_name = "流水线 Webhook"
        verbose_name_plural = verbose_name
        ordering = ['-create_time']
        indexes = [
            models.Index(fields=['pipeline', 'event_type']),
        ]

    def __str__(self):
        return f"{self.pipeline.name} - {self.name}"
