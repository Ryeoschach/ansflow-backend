from django.db import models
from utils.base_model import BaseModel
from django.db.models import JSONField

class WorkerNode(BaseModel):
    """Celery Worker 节点监控"""
    STATUS_CHOICES = (
        ('online', '在线'),
        ('offline', '离线'),
    )
    hostname = models.CharField(max_length=255, unique=True, verbose_name="主机名")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='offline', verbose_name="状态")
    
    # 静态/配置信息
    concurrency = models.IntegerField(default=0, verbose_name="并发数/池大小")
    pool_type = models.CharField(max_length=50, null=True, blank=True, verbose_name="池类型")
    active_queues = JSONField(default=list, verbose_name="监听队列")
    
    # 动态统计
    processed_count = models.IntegerField(default=0, verbose_name="已处理任务数")
    load_avg = JSONField(default=list, verbose_name="负载平均值")
    sw_ver = models.CharField(max_length=100, null=True, blank=True, verbose_name="软件版本")
    sw_sys = models.CharField(max_length=100, null=True, blank=True, verbose_name="系统平台")
    
    last_heartbeat = models.DateTimeField(null=True, blank=True, verbose_name="最后心跳时间")

    class Meta:
        db_table = 'pulse_worker_node'
        verbose_name = "Worker 节点"
        verbose_name_plural = verbose_name

class TaskPulse(BaseModel):
    """Celery 任务脉搏轨迹"""
    STATE_CHOICES = (
        ('PENDING', '等待中'),
        ('RECEIVED', '已接收'),
        ('STARTED', '运行中'),
        ('SUCCESS', '成功'),
        ('FAILURE', '失败'),
        ('REVOKED', '已撤销'),
        ('RETRY', '重试中'),
    )
    task_id = models.CharField(max_length=128, unique=True, verbose_name="任务 ID")
    name = models.CharField(max_length=255, db_index=True, verbose_name="任务名称")
    
    # 消息详情
    args = models.TextField(null=True, blank=True, verbose_name="位置参数")
    kwargs = models.TextField(null=True, blank=True, verbose_name="关键字参数")
    result = models.TextField(null=True, blank=True, verbose_name="执行结果")
    traceback = models.TextField(null=True, blank=True, verbose_name="错误堆栈")
    
    # 路由信息
    routing_key = models.CharField(max_length=255, null=True, blank=True, verbose_name="路由键")
    exchange = models.CharField(max_length=255, null=True, blank=True, verbose_name="交换机")
    
    state = models.CharField(max_length=20, choices=STATE_CHOICES, default='PENDING', verbose_name="任务状态")
    worker = models.ForeignKey(WorkerNode, on_delete=models.SET_NULL, null=True, related_name='tasks', verbose_name="执行节点")
    
    runtime = models.FloatField(null=True, blank=True, verbose_name="运行耗时(秒)")
    
    # 层级关系
    parent_id = models.CharField(max_length=128, null=True, blank=True, db_index=True, verbose_name="父任务 ID")
    
    start_time = models.DateTimeField(null=True, blank=True, verbose_name="开始时间")
    end_time = models.DateTimeField(null=True, blank=True, verbose_name="结束时间")

    class Meta:
        db_table = 'pulse_task_track'
        ordering = ['-create_time']
        verbose_name = "任务脉搏"
        verbose_name_plural = verbose_name
