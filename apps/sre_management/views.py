from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import AllowAny
from django.core.cache import cache
from drf_spectacular.utils import extend_schema
from utils.rbac_permission import SmartRBACPermission
from .models import AlertEvent, SelfHealingPolicy
from .serializers import AlertEventSerializer, SelfHealingPolicySerializer
from .permissions import AlertWebhookPermission


@extend_schema(tags=["SRE 告警管理"])
class AlertEventViewSet(viewsets.ModelViewSet):
    queryset = AlertEvent.objects.all().order_by('-create_time')
    serializer_class = AlertEventSerializer
    permission_classes = [SmartRBACPermission]
    resource_code = "sre:alert"
    resource_type = "sre"
    resource_owner_field = "creator" 
    filterset_fields = {
        'alert_name': ['icontains'],
        'status': ['exact'],
        'severity': ['exact'],
    }

    permission_labels = {
        'bind_healing_pipeline': {'name': '绑定自愈流水线', 'danger': 'warn'},
        'export_to_knowledge': {'name': '导出诊断至知识库', 'danger': 'safe'},
        'trigger_healing': {'name': '触发自愈流水线', 'danger': 'warn'},
        'bulk_destroy': {'name': '批量删除告警', 'danger': 'danger'},
    }

    @action(detail=False, methods=['post'], url_path='bulk-destroy')
    def bulk_destroy(self, request):
        ids = request.data.get('ids', [])
        if not ids:
            return Response({"error": "No IDs provided"}, status=status.HTTP_400_BAD_REQUEST)
        
        count = self.queryset.filter(id__in=ids).delete()[0]
        return Response({"message": f"Successfully deleted {count} alerts"}, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='export-to-knowledge')
    def export_to_knowledge(self, request, pk=None):
        obj = self.get_object()
        if not obj.ai_analysis:
            return Response({"error": "No analysis result to export"}, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            from apps.ai_engine.rag_service import RAGService
            knowledge_content = f"告警类型: {obj.alert_name}\n标签: {obj.labels}\n诊断结论: {obj.ai_analysis}"
            
            rag_service = RAGService()
            rag_service.add_knowledge(
                content=knowledge_content,
                metadata={
                    "source": "alert_diagnosis",
                    "alert_id": obj.id,
                    "fingerprint": obj.fingerprint,
                    "type": "human_verified_knowledge"
                }
            )
            obj.is_exported = True
            obj.save(update_fields=['is_exported'])
            return Response({"message": "Successfully exported to knowledge base"}, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=['post'], url_path='re-diagnose')
    def re_diagnose(self, request, pk=None):
        """重新诊断失败的自愈流水线"""
        obj = self.get_object()
        if not obj.latest_run_id:
            return Response({"error": "没有可供诊断的历史运行记录"}, status=status.HTTP_400_BAD_REQUEST)
        
        from apps.pipeline_management.models import PipelineRun, PipelineNodeRun
        run = PipelineRun.objects.filter(id=obj.latest_run_id).first()
        if not run or run.status != 'failed':
            return Response({"error": "最新运行记录并非失败状态，无需重诊"}, status=status.HTTP_400_BAD_REQUEST)
            
        failed_node = PipelineNodeRun.objects.filter(run=run, status='failed').first()
        if not failed_node or not failed_node.logs:
            return Response({"error": "未找到具体的失败节点或缺失执行日志"}, status=status.HTTP_400_BAD_REQUEST)
            
        # 截取最后 1000 字符的日志
        log_content = failed_node.logs[-1000:]
        context_info = {"type": failed_node.node_type, "name": failed_node.node_label, "summary": f"自愈流水线节点失败: {failed_node.node_label}"}
        
        from apps.ai_engine.rag_service import RAGService
        try:
            rag = RAGService()
            res = ""
            for chunk in rag.diagnose_log(log_content, context_info):
                res += chunk
            
            obj.ai_analysis = (obj.ai_analysis or "") + f"\n\n=== 失败重诊记录 (Run #{run.id}) ===\n{res}"
            obj.healing_status = 'suggested'
            obj.save(update_fields=['ai_analysis', 'healing_status'])
            return Response({"message": "重新诊断完成"}, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=['post'], url_path='trigger-healing')
    def trigger_healing(self, request, pk=None):
        obj = self.get_object()
        if not obj.suggested_pipeline:
            return Response({"error": "No suggested pipeline for this alert"}, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            from apps.pipeline_management.models import PipelineRun
            from apps.pipeline_management.tasks import advance_pipeline_engine
            from django.db import transaction
            
            with transaction.atomic():
                run = PipelineRun.objects.create(
                    pipeline=obj.suggested_pipeline,
                    status='pending',
                    trigger_user=request.user,
                    trigger_type='manual'
                )
                obj.latest_run_id = run.id
                obj.trigger_type = 'manual'
                obj.healing_status = 'executing'
                obj.save(update_fields=['latest_run_id', 'trigger_type', 'healing_status'])
                
                transaction.on_commit(lambda: advance_pipeline_engine.delay(run.id))
            
            return Response({"message": "Healing pipeline triggered", "run_id": run.id}, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @extend_schema(responses={200: dict}, description="接收 Prometheus 告警 Webhook")
    @action(detail=False, methods=['post'], permission_classes=[AlertWebhookPermission], authentication_classes=[])
    def receive(self, request):
        data = request.data
        alerts = data.get('alerts', [])
        
        for alert in alerts:
            # 提取指纹，用于幂等
            fingerprint = alert.get('fingerprint')
            alert_status = alert.get('status', 'firing')
            labels = alert.get('labels', {})
            annotations = alert.get('annotations', {})
            alert_name = labels.get('alertname', 'Unknown Alert')
            severity = labels.get('severity', 'warning')

            # 存入数据库
            old_obj = AlertEvent.objects.filter(fingerprint=fingerprint).first()
            should_analyze = False
            
            if alert_status == 'firing':
                if not old_obj or old_obj.status != 'firing' or old_obj.healing_status in ['none', 'failed']:
                    should_analyze = True

            obj, created = AlertEvent.objects.update_or_create(
                fingerprint=fingerprint,
                defaults={
                    'alert_name': alert_name,
                    'severity': severity,
                    'status': alert_status,
                    'labels': labels,
                    'annotations': annotations,
                }
            )
            
            # 触发 AI 分析 Celery 任务 (引入 Redis 防抖)
            if should_analyze:
                lock_key = f"alert_analysis_lock_{fingerprint}"
                if cache.add(lock_key, True, timeout=300):
                    obj.healing_status = 'analyzing'
                    obj.save(update_fields=['healing_status'])
                    from .tasks import analyze_alert_event
                    analyze_alert_event.delay(obj.id)
                else:
                    print(f"[SRE] Skipped analysis for fingerprint {fingerprint} due to debouncing.")
            
        return Response({"message": f"Successfully received {len(alerts)} alerts"}, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='bind-healing-pipeline')
    def bind_healing_pipeline(self, request, pk=None):
        """
        将流水线绑定到告警作为建议方案，并可选地创建/更新自愈策略
        """
        obj = self.get_object()
        pipeline_id = request.data.get('pipeline_id')
        make_policy = request.data.get('make_policy', False)
        
        if not pipeline_id:
            return Response({"error": "pipeline_id is required"}, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            from apps.pipeline_management.models import Pipeline
            pipeline = Pipeline.objects.get(id=pipeline_id)
            
            # 1. 绑定建议流水线
            obj.suggested_pipeline = pipeline
            if obj.healing_status == 'none':
                obj.healing_status = 'suggested'
            obj.save(update_fields=['suggested_pipeline', 'healing_status'])
            
            # 2. 如果要求创建策略
            if make_policy:
                # 使用告警的 labels 作为匹配规则
                # 排除一些过于具体的标签（如 pod 名称、instance IP 等，视具体业务而定）
                # 这里简单处理，取所有标签
                policy, created = SelfHealingPolicy.objects.update_or_create(
                    alert_match_rule=obj.labels,
                    defaults={
                        'name': f"Auto policy for {obj.alert_name}",
                        'pipeline': pipeline,
                        'is_active': True
                    }
                )
                return Response({
                    "message": "Pipeline bound and policy created",
                    "policy_id": policy.id
                }, status=status.HTTP_200_OK)

            return Response({"message": "Pipeline bound to alert successfully"}, status=status.HTTP_200_OK)
        except Pipeline.DoesNotExist:
            return Response({"error": "Pipeline not found"}, status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

@extend_schema(tags=["SRE 自愈策略"])
class SelfHealingPolicyViewSet(viewsets.ModelViewSet):
    queryset = SelfHealingPolicy.objects.all().order_by('-create_time')
    serializer_class = SelfHealingPolicySerializer
    permission_classes = [SmartRBACPermission]
    resource_code = "sre:policy"
    resource_type = "sre"
    filterset_fields = {
        'name': ['icontains'],
        'is_active': ['exact'],
    }

    permission_labels = {
        'bulk_destroy': {'name': '批量删除策略', 'danger': 'danger'},
    }

    @action(detail=False, methods=['post'], url_path='bulk-destroy')
    def bulk_destroy(self, request):
        ids = request.data.get('ids', [])
        if not ids:
            return Response({"error": "No IDs provided"}, status=status.HTTP_400_BAD_REQUEST)
        
        count = self.queryset.filter(id__in=ids).delete()[0]
        return Response({"message": f"Successfully deleted {count} policies"}, status=status.HTTP_200_OK)
