from rest_framework import viewsets, permissions
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework import status # Added for status.HTTP_400_BAD_REQUEST
from django.utils import timezone
from config.celery import app as celery_app
from .models import Pipeline, PipelineRun, CIEnvironment, PipelineNodeRun, PipelineWebhook, PipelineVersion
from .serializers import PipelineSerializer, PipelineRunSerializer, CIEnvironmentSerializer, PipelineWebhookSerializer, PipelineVersionSerializer
from utils.rbac_permission import SmartRBACPermission, DataScopeMixin

from apps.pipeline_management.tasks import advance_pipeline_engine, push_pipeline_status_to_ws


def get_ancestors(node_id, edges):
    """
    BFS 反向遍历，返回所有前置节点 ID
    edges: [{'source': 'dndnode_0', 'target': 'dndnode_1'}, ...]
    """
    ancestors = []
    visited = set()
    queue = [node_id]

    while queue:
        current = queue.pop(0)
        if current in visited:
            continue
        visited.add(current)

        # 找所有指向 current 的边（current 是 target）
        parents = [e['source'] for e in edges if e.get('target') == current]
        for p in parents:
            if p not in visited:
                ancestors.append(p)
                queue.append(p)

    return ancestors

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
            trigger_user=request.user,
            trigger_type='manual'
        )
        
        # 触发 Celery DAG 引擎进行拓扑遍历调度
        advance_pipeline_engine.delay(run.id)
        
        return Response({'msg': '流水线已启动', 'run_id': run.id})

    @action(detail=True, methods=['post'])
    def rollback(self, request, pk=None):
        """
        回滚流水线到指定版本
        POST /api/v1/pipelines/{id}/rollback/
        Body: { "version_id": 3 }
        """
        pipeline = self.get_object()
        version_id = request.data.get('version_id')

        if not version_id:
            return Response({'error': '必须指定 version_id'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            version = pipeline.versions.get(id=version_id)
        except PipelineVersion.DoesNotExist:
            return Response({'error': '指定版本不存在'}, status=status.HTTP_404_NOT_FOUND)

        # 恢复 graph_data 和描述
        pipeline.graph_data = version.graph_data
        pipeline.name = version.name
        pipeline.desc = version.desc
        pipeline.save()

        return Response({'msg': f'已回滚到 v{version.version_number}', 'pipeline_id': pipeline.id})

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

    @action(detail=True, methods=['post'])
    def retry(self, request, pk=None):
        """
        从指定节点重试流水线
        POST /api/v1/pipeline_runs/{run_id}/retry/
        Body: { "start_node_id": "dndnode_2" }  // 可选，空字符串从头重试
        """
        parent_run = self.get_object()

        # 仅允许对 failed 状态的 Run 进行重试
        if parent_run.status != 'failed':
            return Response(
                {"error": "只有执行失败的流水线才能重试"},
                status=status.HTTP_400_BAD_REQUEST
            )

        start_node_id = request.data.get('start_node_id', '')

        # 获取 DAG 拓扑信息
        graph_data = parent_run.pipeline.graph_data
        nodes = graph_data.get('nodes', [])
        edges = graph_data.get('edges', [])

        # 构建 node_id -> node 映射
        node_map = {n['id']: n for n in nodes}

        # 确定起始节点（默认为 DAG 第一个节点，通常是 type='input' 的 Trigger 节点）
        if start_node_id and start_node_id in node_map:
            actual_start_node = start_node_id
        else:
            # 找到入口节点（没有上游的节点）
            targets = {e.get('target') for e in edges}
            entry_nodes = [n['id'] for n in nodes if n['id'] not in targets]
            actual_start_node = entry_nodes[0] if entry_nodes else (nodes[0]['id'] if nodes else None)

        if not actual_start_node:
            return Response({"error": "无法确定起始节点"}, status=status.HTTP_400_BAD_REQUEST)

        # 计算需要跳过的前置节点
        ancestors = get_ancestors(actual_start_node, edges)

        # 获取父 Run 的节点执行结果
        parent_node_results = {n.node_id: n for n in parent_run.nodes.all()}

        # 创建新的 PipelineRun
        new_run = PipelineRun.objects.create(
            pipeline=parent_run.pipeline,
            status='pending',
            trigger_user=request.user,
            trigger_type='retry',
            parent_run=parent_run,
            start_node_id=actual_start_node,
        )

        # 创建节点状态记录
        for node in nodes:
            node_id = node['id']
            parent_result = parent_node_results.get(node_id)

            if node_id in ancestors:
                # 前置节点：跳过，复用上次结果
                PipelineNodeRun.objects.create(
                    run=new_run,
                    node_id=node_id,
                    node_type=node.get('type', ''),
                    node_label=node.get('data', {}).get('label', ''),
                    status='skipped',
                    logs=parent_result.logs if parent_result else '',
                    start_time=parent_result.start_time if parent_result else None,
                    end_time=parent_result.end_time if parent_result else None,
                )
            else:
                # 从起始节点开始，需要重新执行
                PipelineNodeRun.objects.create(
                    run=new_run,
                    node_id=node_id,
                    node_type=node.get('type', ''),
                    node_label=node.get('data', {}).get('label', ''),
                    status='pending',
                )

        # 触发异步执行
        advance_pipeline_engine.delay(new_run.id)

        # 返回新 Run 信息
        serializer = self.get_serializer(new_run)
        return Response(serializer.data, status=status.HTTP_202_ACCEPTED)

class CIEnvironmentViewSet(viewsets.ModelViewSet):
    """构建镜像环境管理"""
    queryset = CIEnvironment.objects.all()
    serializer_class = CIEnvironmentSerializer
    permission_classes = [SmartRBACPermission]
    resource_code = 'pipeline:ci_env'


class PipelineWebhookViewSet(DataScopeMixin, viewsets.ModelViewSet):
    """流水线 Webhook 配置管理"""
    queryset = PipelineWebhook.objects.all()
    serializer_class = PipelineWebhookSerializer
    permission_classes = [SmartRBACPermission]
    resource_code = 'pipeline:webhook'
    resource_type = 'pipeline'
    resource_owner_field = 'pipeline__creator'
    filterset_fields = ['pipeline', 'event_type', 'is_active']
    search_fields = ['name', 'repository_url']
    ordering_fields = ['create_time', 'last_trigger_time']

    def perform_create(self, serializer):
        serializer.save()

    @action(detail=True, methods=['post'], url_path='trigger')
    def trigger(self, request, pk=None):
        """
        触发 Webhook 对应的流水线（供外部系统调用，不需要认证）
        GET/POST /api/v1/pipeline/webhooks/{id}/trigger/?secret=xxx
        """
        webhook = self.get_object()

        # 验证 secret
        secret = request.query_params.get('secret') or request.data.get('secret')
        if webhook.secret_key and webhook.secret_key != secret:
            return Response({'error': 'Invalid secret'}, status=status.HTTP_403_FORBIDDEN)

        # 检查是否启用
        if not webhook.is_active:
            return Response({'error': 'Webhook is disabled'}, status=status.HTTP_400_BAD_REQUEST)

        # 获取事件类型和分支信息
        event_type = request.data.get('event') or request.query_params.get('event', 'push')
        branch = request.data.get('ref', '').replace('refs/heads/', '') or request.query_params.get('branch', '')

        # 检查分支过滤
        if webhook.branch_filter:
            import fnmatch
            if not fnmatch.fnmatch(branch, webhook.branch_filter):
                return Response({'message': 'Branch does not match filter, skipped'}, status=status.HTTP_200_OK)

        # 触发流水线
        try:
            run = PipelineRun.objects.create(
                pipeline=webhook.pipeline,
                status='pending',
                trigger_user=webhook.pipeline.creator,
                trigger_type='webhook'
            )

            # 更新触发统计
            webhook.last_trigger_time = timezone.now()
            webhook.trigger_count += 1
            webhook.save()

            # 异步触发 DAG 引擎
            advance_pipeline_engine.delay(run.id)

            return Response({
                'message': 'Pipeline triggered successfully',
                'run_id': run.id,
                'pipeline': webhook.pipeline.name,
                'branch': branch,
            }, status=status.HTTP_202_ACCEPTED)
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class PipelineVersionViewSet(DataScopeMixin, viewsets.ReadOnlyModelViewSet):
    """
    流水线版本历史（只读）
    """
    queryset = PipelineVersion.objects.all()
    serializer_class = PipelineVersionSerializer
    permission_classes = [SmartRBACPermission]
    resource_code = 'pipeline:version'
    resource_type = 'pipeline'
    filterset_fields = ['pipeline', 'is_current']
    search_fields = ['pipeline__name', 'change_summary']
    ordering_fields = ['version_number', 'create_time']

    def get_queryset(self):
        qs = super().get_queryset()
        pipeline_id = self.request.query_params.get('pipeline')
        if pipeline_id:
            qs = qs.filter(pipeline_id=pipeline_id)
        return qs
