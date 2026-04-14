from rest_framework import viewsets, status
from rest_framework.response import Response
from rest_framework.decorators import action
from apps.task_management.models import AnsibleTask, AnsibleExecution
from apps.task_management.serializers import AnsibleTaskSerializer, AnsibleExecutionSerializer, TaskLogSerializer
from apps.task_management.tasks import run_ansible_task
from apps.task_management.filters import AnsibleTaskFilter, AnsibleExecutionFilter
from config.celery import app as celery_app
from django.utils import timezone
from utils.rbac_permission import SmartRBACPermission, DataScopeMixin
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters


class AnsibleTaskViewSet(DataScopeMixin, viewsets.ModelViewSet):
    """
    Ansible 任务模板 (Job Templates)
    """
    queryset = AnsibleTask.objects.all().order_by('-create_time')
    serializer_class = AnsibleTaskSerializer
    permission_classes = [SmartRBACPermission]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter, filters.SearchFilter]
    filterset_class = AnsibleTaskFilter
    search_fields = ['name']
    ordering_fields = ['create_time', 'update_time']
    resource_type = 'ansible_task'
    resource_owner_field = 'creator'
    resource_code = 'tasks:ansible_tasks'
    permission_labels = {
        'view':   {'name': '查看任务模板列表', 'danger': 'safe'},
        'add':    {'name': '新建任务模板',     'danger': 'warn'},
        'edit':   {'name': '编辑任务配置',     'danger': 'warn'},
        'delete': {'name': '删除任务模板',     'danger': 'high'},
        'run':    {'name': '触发执行任务',     'danger': 'warn', 'desc': '允许用户推送一次 Ansible playbook 运行'},
    }

    def perform_create(self, serializer):
        # 保存时自动关联创建者
        task = serializer.save(creator=self.request.user)
        
        # 如果请求中带有 run_now，则立即触发一次执行
        if self.request.data.get('run_now'):
            execution = AnsibleExecution.objects.create(
                task=task,
                executor=self.request.user,
                status='pending'
            )
            res = run_ansible_task.delay(execution.id)
            execution.celery_task_id = res.id
            execution.save()

    @action(detail=True, methods=['post'])
    def run(self, request, pk=None):
        """
        触发该模板运行，生成执行记录实例
        """
        task = self.get_object()
        execution = AnsibleExecution.objects.create(
            task=task,
            executor=request.user,
            status='pending'
        )
        res = run_ansible_task.delay(execution.id)
        execution.celery_task_id = res.id
        execution.save()

        return Response({
            "message": "任务执行已触发",
            "execution_id": execution.id
        }, status=status.HTTP_201_CREATED)


class AnsibleExecutionViewSet(DataScopeMixin, viewsets.ModelViewSet):
    """
    Ansible 执行记录 (Auditing/History)
    """
    queryset = AnsibleExecution.objects.all().order_by('-create_time')
    serializer_class = AnsibleExecutionSerializer
    permission_classes = [SmartRBACPermission]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_class = AnsibleExecutionFilter
    ordering_fields = ['create_time', 'start_time', 'end_time']
    resource_type = 'ansible_task'
    resource_lookup_field = 'task_id'
    resource_owner_field = 'executor'
    resource_code = 'tasks:ansible_executions'
    permission_labels = {
        'view':         {'name': '查看执行历史',   'danger': 'safe'},
        'delete':       {'name': '删除单条历史',   'danger': 'high'},
        'batch_delete': {'name': '批量清理历史',   'danger': 'high', 'desc': '批量删除指定的 Ansible 执行记录'},
        'terminate':    {'name': '强杀进行中的任务', 'danger': 'high', 'desc': '向 Celery 发送 SIGKILL 信号强制停止'},
        'logs':         {'name': '查看执行日志',   'danger': 'safe'},
    }

    @action(detail=True, methods=['get'])
    def logs(self, request, pk=None):
        """
        查看某次执行的具体日志
        """
        execution = self.get_object()
        logs = execution.logs.all().order_by('create_time')
        serializer = TaskLogSerializer(logs, many=True)
        return Response(serializer.data)
    
    @action(detail=False, methods=['delete'])
    def batch_delete(self, request):
        """
        清理历史记录
        """
        ids = request.data.get('ids', [])
        if not ids:
            return Response({"error": "请提供要删除的 ID 列表"}, status=status.HTTP_400_BAD_REQUEST)
        
        AnsibleExecution.objects.filter(id__in=ids).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=['post'])
    def terminate(self, request, pk=None):
        """
        强制终止正在执行或排队中的 Ansible 任务
        """
        execution = self.get_object()
        if execution.status not in ['pending', 'running']:
            return Response({"error": "该任务当前不处于可停止状态"}, status=status.HTTP_400_BAD_REQUEST)
        
        # 1. 向 Celery 发送终止信号
        if execution.celery_task_id:
            celery_app.control.revoke(execution.celery_task_id, terminate=True, signal='SIGKILL')
        
        # 2. 更新数据库状态
        execution.status = 'failed'
        execution.end_time = timezone.now()
        from apps.task_management.models import TaskLog
        TaskLog.objects.create(
            execution=execution, 
            host="SYSTEM", 
            output="\n[!] 任务已被用户手动强制终止 (Revoked by User)"
        )
        execution.save()
        
        return Response({"message": "终止指令已下发"})
