from rest_framework import viewsets, permissions
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework import status # Added for status.HTTP_400_BAD_REQUEST
from django.utils import timezone
from config.celery import app as celery_app
from .models import Pipeline, PipelineRun, CIEnvironment, PipelineNodeRun
from .serializers import PipelineSerializer, PipelineRunSerializer, CIEnvironmentSerializer
from utils.rbac_permission import SmartRBACPermission, DataScopeMixin

from apps.pipeline_management.tasks import advance_pipeline_engine, push_pipeline_status_to_ws

class PipelineViewSet(DataScopeMixin, viewsets.ModelViewSet):
    queryset = Pipeline.objects.all()
    serializer_class = PipelineSerializer
    permission_classes = [SmartRBACPermission]
    resource_code = 'pipeline:template'
    resource_type = 'pipeline'
    resource_owner_field = 'creator'
    permission_labels = {
        'view':    {'name': '查看流水线列表/详情', 'danger': 'safe'},
        'add':     {'name': '新建流水线模板',     'danger': 'warn'},
        'edit':    {'name': '编辑流水线配置',     'danger': 'warn'},
        'delete':  {'name': '删除流水线模板',     'danger': 'high'},
        'execute': {'name': '触发流水线执行',     'danger': 'warn', 'desc': '允许手动启动一条流水线'},
    }

    def get_queryset(self):
        qs = super().get_queryset()
        has_cron = self.request.query_params.get('has_cron')
        if has_cron == 'true':
            # 只返回配置了定时表达式的流水线
            return qs.exclude(cron_expression__isnull=True).exclude(cron_expression='')
        return qs

    def perform_create(self, serializer):
        serializer.save(creator=self.request.user)

    @action(detail=True, methods=['post'])
    def execute(self, request, pk=None):
        """触发执行流水线"""
        
        # 👑 【核心介入】挂载审批代理探针
        from apps.approval_center.engine import ProxyApprovalEngine
        is_blocked, approval_res = ProxyApprovalEngine.intercept_if_needed(
            request, 
            resource_type='pipeline:execute', 
            action_title=f"申请运行流水线模板 #{pk}",
            target_id=pk
        )
        # 如果命中安全策略，引擎将其冻结并打包为工单，直接返回 202 挂起响应
        if is_blocked:
            return approval_res

        pipeline = self.get_object()
        run = PipelineRun.objects.create(
            pipeline=pipeline,
            status='pending',
            trigger_user=request.user
        )
        
        # 触发 Celery DAG 引擎进行拓扑遍历调度
        advance_pipeline_engine.delay(run.id)
        
        return Response({'msg': '流水线已启动', 'run_id': run.id})

class PipelineRunViewSet(DataScopeMixin, viewsets.ModelViewSet):
    """流水线运行记录"""
    queryset = PipelineRun.objects.all()
    serializer_class = PipelineRunSerializer
    permission_classes = [SmartRBACPermission]
    resource_code = 'pipeline:run'
    resource_type = 'pipeline'
    resource_lookup_field = 'pipeline_id'
    resource_owner_field = 'trigger_user'
    permission_labels = {
        'view':   {'name': '查看执行历史',       'danger': 'safe'},
        'add':    {'name': '手动创建执行记录',   'danger': 'warn'},
        'edit':   {'name': '修改执行记录',       'danger': 'warn'},
        'delete': {'name': '删除执行历史',       'danger': 'high'},
        'stop':   {'name': '强制终止流水线实例', 'danger': 'high', 'desc': '允许用户中断正在运行的流水线'},
    }
    filterset_fields = ['pipeline', 'status']
    search_fields = ['pipeline__name', 'trigger_user__username', 'id']

    @action(detail=True, methods=['post'])
    def stop(self, request, pk=None):
        """取消/停止整条流水线"""
        run = self.get_object()
        if run.status not in ['pending', 'running']:
            return Response({"error": "流水线已完成或不可停止"}, status=status.HTTP_400_BAD_REQUEST)
        
        # 1. 标记取消（拦截 DAG 引擎下一步调度）
        run.status = 'cancelled'
        run.end_time = timezone.now()
        run.save()
        
        # 实时推送：流水线已被取消
        push_pipeline_status_to_ws(run)

        # 2. 杀掉大脑 (DAG)
        if run.celery_task_id:
            celery_app.control.revoke(run.celery_task_id, terminate=True, signal='SIGKILL')

        # 3. 杀掉所有当前正在跑/等待的节点
        # 确保涵盖所有非终态：无论是 running 还是处于等待队列 pending 的
        non_final_nodes = run.nodes.exclude(status__in=['success', 'failed', 'skipped'])
        
        from django.apps import apps
        AnsibleExecution = apps.get_model('task_management', 'AnsibleExecution')
        from django.db.models import Q
        
        for node in non_final_nodes:
            # 杀掉 Celery 节点任务 (如果是正在跑的)
            if node.celery_task_id and node.status == 'running':
                celery_app.control.revoke(node.celery_task_id, terminate=True, signal='SIGKILL')
            
            # 特殊处理：如果是关联了 Ansible 的节点，显式关停底层任务
            if node.node_type == 'ansible':
                # 先尝试通过 output_data 的精确 ID 匹配（针对新任务）
                ex_id = node.output_data.get('ansible_execution_id') if node.output_data else None
                
                # 如果找不到精确 ID，则通过 触发人+时间+状态 进行模糊匹配（针对旧任务或极端重试情况）
                if ex_id:
                    executions = AnsibleExecution.objects.filter(id=ex_id)
                else:
                    executions = AnsibleExecution.objects.filter(
                        executor=run.trigger_user,
                        status__in=['pending', 'running'],
                        create_time__gte=run.start_time
                    )

                for ex in executions:
                    if ex.status in ['pending', 'running']:
                        ex.status = 'failed'
                        ex.remark = f"所属流水线实例#{run.id}已被手动取消，关联任务强制关停。"
                        ex.end_time = timezone.now()
                        ex.save()
                        # 确保也撤回该执行实例对应的 Celery 任务 ID
                        if ex.celery_task_id:
                            celery_app.control.revoke(ex.celery_task_id, terminate=True, signal='SIGKILL')

            # 强制回填节点状态与结束时间
            node.status = 'cancelled'
            node.logs = (node.logs or "") + f"\n[!] 🚨 警告: 任务执行于 {timezone.now().strftime('%Y-%m-%d %H:%M:%S')} 被手动强制取消。"
            node.end_time = timezone.now()
            node.save()

        return Response({"message": "中止指令已发，相关节点及底层 Ansible 任务已强制关停并清理状态。"})

class CIEnvironmentViewSet(viewsets.ModelViewSet):
    """构建镜像环境管理"""
    queryset = CIEnvironment.objects.all()
    serializer_class = CIEnvironmentSerializer
    permission_classes = [SmartRBACPermission]
    resource_code = 'pipeline:ci_env'
