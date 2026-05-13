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
    
    @action(detail=True, methods=['post'])
    def revoke(self, request, pk=None):
        """撤销任务"""
        instance = self.get_object()
        celery_app.control.revoke(instance.task_id, terminate=True)
        return Response({'status': 'revoking', 'task_id': instance.task_id})

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
