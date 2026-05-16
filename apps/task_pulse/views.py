from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from .models import WorkerNode, TaskPulse
from .serializers import WorkerNodeSerializer, TaskPulseSerializer
from utils.rbac_permission import SmartRBACPermission
from utils.pagination import MyCustomPagination
from config.celery import app as celery_app

class TaskPulseViewSet(viewsets.ReadOnlyModelViewSet):
    """任务脉搏实时追踪 API"""
    queryset = TaskPulse.objects.select_related('worker').all()
    serializer_class = TaskPulseSerializer
    permission_classes = [SmartRBACPermission]
    pagination_class = MyCustomPagination
    resource_code = "task:pulse"
    filterset_fields = ['state', 'worker']
    
    @action(detail=False, methods=['get'])
    def throughput(self, request):
        """过去 24 小时任务吞吐量统计"""
        from django.utils import timezone
        from datetime import timedelta
        from django.db.models.functions import TruncHour
        from django.db.models import Count
        
        last_24h = timezone.now() - timedelta(hours=24)
        stats = TaskPulse.objects.filter(create_time__gte=last_24h)\
            .annotate(hour=TruncHour('create_time'))\
            .values('hour')\
            .annotate(count=Count('id'))\
            .order_by('hour')
            
        data = []
        # 填充 24 小时的空点，确保图表平滑
        for i in range(24):
            time_point = (timezone.now() - timedelta(hours=23-i)).replace(minute=0, second=0, microsecond=0)
            count = 0
            for s in stats:
                if s['hour'] == time_point:
                    count = s['count']
                    break
            data.append({
                "time": time_point.strftime("%H:00"),
                "value": count
            })
            
        return Response(data)

    @action(detail=True, methods=['post'])
    def revoke(self, request, pk=None):
        """撤销任务"""
        instance = self.get_object()
        
        # 1. 发送撤销指令给 Celery
        try:
            celery_app.control.revoke(instance.task_id, terminate=True)
        except Exception as e:
            logger.warning(f"Failed to send revoke command to Celery for {instance.task_id}: {e}")

        # 2. 立即更新数据库状态 (防止 Monitor 未及时处理事件)
        instance.state = 'REVOKED'
        instance.end_time = timezone.now()
        instance.save(update_fields=['state', 'end_time'])

        # 3. [增强] 尝试同步更新关联的业务记录
        # 如果是流水线任务
        if "pipeline" in instance.name:
            from apps.pipeline_management.models import PipelineRun
            # 假设 task_id 存储在 PipelineRun 中 (通常在创建时记录)
            PipelineRun.objects.filter(celery_task_id=instance.task_id).update(status='cancelled')
        
        # 如果是 Ansible 任务
        if "ansible" in instance.name:
            from apps.task_management.models import AnsibleExecution
            AnsibleExecution.objects.filter(celery_task_id=instance.task_id).update(status='cancelled')

        return Response({
            'status': 'revoked', 
            'task_id': instance.task_id,
            'message': '任务已撤销并已更新数据库状态'
        })

class WorkerNodeViewSet(viewsets.ReadOnlyModelViewSet):
    """Worker 节点状态 API"""
    queryset = WorkerNode.objects.all()
    serializer_class = WorkerNodeSerializer
    permission_classes = [SmartRBACPermission]
    resource_code = "task:worker"

    @action(detail=True, methods=['get'])
    def detail_info(self, request, pk=None):
        """获取 Worker 实时详细信息 (通过 Inspect)"""
        instance = self.get_object()
        inspector = celery_app.control.inspect([instance.hostname])
        
        stats = inspector.stats() or {}
        active = inspector.active() or {}
        registered = inspector.registered() or {}
        
        return Response({
            'db_info': self.get_serializer(instance).data,
            'realtime_stats': stats.get(instance.hostname),
            'active_tasks': active.get(instance.hostname, []),
            'registered_tasks': registered.get(instance.hostname, []),
        })

    @action(detail=False, methods=['get'])
    def stats(self, request):
        """集群概览"""
        return Response({
            'total_workers': WorkerNode.objects.count(),
            'online_workers': WorkerNode.objects.filter(status='online').count(),
            'total_tasks': TaskPulse.objects.count(),
            'running_tasks': TaskPulse.objects.filter(state='STARTED').count(),
        })
