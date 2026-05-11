from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import AllowAny
from drf_spectacular.utils import extend_schema
from utils.rbac_permission import SmartRBACPermission
from .models import AlertEvent, SelfHealingPolicy
from .serializers import AlertEventSerializer, SelfHealingPolicySerializer

@extend_schema(tags=["SRE 告警管理"])
class AlertEventViewSet(viewsets.ModelViewSet):
    queryset = AlertEvent.objects.all()
    serializer_class = AlertEventSerializer
    permission_classes = [SmartRBACPermission]
    resource_code = "sre:alert"
    resource_type = "sre"
    resource_owner_field = "creator" # 告警通常是系统产生的

    permission_labels = {
        'bind_healing_pipeline': {'name': '绑定自愈流水线', 'danger': 'warn'},
        'export_to_knowledge': {'name': '导出诊断至知识库', 'danger': 'safe'},
        'trigger_healing': {'name': '触发自愈流水线', 'danger': 'warn'},
    }

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
    @action(detail=False, methods=['post'], permission_classes=[AllowAny]) # 告警网关设为 AllowAny，但建议后续增加 Token 校验
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
            obj, created = AlertEvent.objects.update_or_create(
                fingerprint=fingerprint,
                defaults={
                    'alert_name': alert_name,
                    'severity': severity,
                    'status': alert_status,
                    'labels': labels,
                    'annotations': annotations,
                    'healing_status': 'analyzing' if alert_status == 'firing' else 'none'
                }
            )
            
            # 触发 AI 分析 Celery 任务
            if alert_status == 'firing':
                from .tasks import analyze_alert_event
                analyze_alert_event.delay(obj.id)
            
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
    queryset = SelfHealingPolicy.objects.all()
    serializer_class = SelfHealingPolicySerializer
    permission_classes = [SmartRBACPermission]
    resource_code = "sre:policy"
    resource_type = "sre"
