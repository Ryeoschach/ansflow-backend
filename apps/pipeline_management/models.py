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
        # 1. 拦截保存动作，先把对象存到数据库从而产生 ID
        super(Pipeline, self).save(*args, **kwargs)

        # 2. 如果成功安装了 django_celery_beat，则注册/更新它的定期任务
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


class PipelineRun(BaseModel):
    """单次流水线运行实例"""
    STATUS_CHOICES = (
        ('pending', '等待执行'),
        ('running', '运行中'),
        ('success', '执行成功'),
        ('failed', '执行失败'),
        ('cancelled', '已取消'),
    )
    pipeline = models.ForeignKey(Pipeline, on_delete=models.CASCADE, related_name='runs')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    trigger_user = models.ForeignKey('rbac_permission.User', on_delete=models.SET_NULL, null=True, blank=True)
    celery_task_id = models.CharField(max_length=128, null=True, blank=True, verbose_name="DAG 任务 ID")
    start_time = models.DateTimeField(null=True, blank=True)
    end_time = models.DateTimeField(null=True, blank=True)

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
