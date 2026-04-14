from django.db import models
from apps.rbac_permission.models import User, Role

class ApprovalPolicy(models.Model):
    """
    审批策略：定义在什么资源、什么环境下触发全局拦截与审批。
    """
    name = models.CharField(max_length=100, verbose_name="策略名称")
    resource_type = models.CharField(max_length=100, verbose_name="资源类型", help_text="如 pipeline:run, ansible:execution")
    environment = models.CharField(max_length=100, null=True, blank=True, verbose_name="生效环境", help_text="如 PROD。如果为空，则表示拦截该资源下所有环境的操作。")
    approver_roles = models.ManyToManyField(Role, blank=True, verbose_name="指定的审批角色集合")
    is_active = models.BooleanField(default=True, verbose_name="是否启用")
    create_time = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")

    class Meta:
        db_table = 'approval_policy'
        verbose_name = '审批策略'
        verbose_name_plural = verbose_name


class ApprovalTicket(models.Model):
    """
    审批工单：保存被拦截的高危任务上下文，待审批后放行（代理恢复执行）。
    """
    STATUS_CHOICES = (
        ('pending', '待审批'),
        ('approved', '已批准 (后台发往引擎)'),
        ('rejected', '已驳回'),
        ('canceled', '发起人已撤销'),
        ('finished', '已执行 (放行成功)'),
        ('failed', '执行失败 (放行触发报错)'),
    )

    title = models.CharField(max_length=255, verbose_name="审批标题", help_text="如: 生产环境_WebCore_主干发布")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', verbose_name="审批状态")
    
    submitter = models.ForeignKey(User, on_delete=models.CASCADE, related_name='submitted_approvals', verbose_name="发起人工号")
    approver = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='approved_approvals', verbose_name="实际签署人")
    
    # Payload 代理机制核心
    resource_type = models.CharField(max_length=100, verbose_name="目标组件", help_text="标记要请求哪个底层模块")
    target_id = models.CharField(max_length=64, null=True, blank=True, verbose_name="如果有特定的资源 ID，则记录")
    payload = models.JSONField(verbose_name="原汁原味的 Request Body")
    url_path = models.CharField(max_length=255, verbose_name="拦截发往的 URL", help_text="通过这个端点，系统可以代替发起人放行请求")
    method = models.CharField(max_length=10, default='POST', verbose_name="HTTP动词")
    
    # 追溯流言
    remark = models.TextField(null=True, blank=True, verbose_name="审批意见 / 为什么驳回")
    create_time = models.DateTimeField(auto_now_add=True, verbose_name="发起时间")
    audit_time = models.DateTimeField(null=True, blank=True, verbose_name="签批时间")

    class Meta:
        db_table = 'approval_ticket'
        verbose_name = '审批工单记录'
        verbose_name_plural = verbose_name
        ordering = ['-create_time']
