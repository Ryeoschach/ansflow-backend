from datetime import timedelta

from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import AllowAny
from rest_framework.exceptions import PermissionDenied
from django.core.cache import cache
from django.db import models, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from drf_spectacular.utils import extend_schema
from utils.rbac_permission import SmartRBACPermission, DataScopeMixin
from .models import (
    AlertEvent,
    DiagnosisFeedback,
    DiagnosisReplayCase,
    DiagnosisReplayResult,
    DiagnosisRun,
    DiagnosisTemplate,
    DiagnosisTemplateVersion,
    ObservabilityDataSource,
    ObservedService,
    SelfHealingPolicy,
)
from .diagnosis_quality import (
    create_template_version,
    diagnosis_quality_summary,
)
from .diagnosis_security import redact_sensitive_data
from .diagnosis_utils import match_services_for_alert
from .observability import get_datasource_capabilities, get_log_adapter, get_metric_adapter, get_observability_adapter
from .rule_templates import list_templates, render_template
from .serializers import (
    AlertEventSerializer,
    AlertRuleTemplateRenderRequestSerializer,
    AlertRuleTemplateRenderSerializer,
    AlertRuleTemplateSerializer,
    DiagnosisRunSerializer,
    DiagnosisRunListSerializer,
    DiagnosisFeedbackSerializer,
    DiagnosisReplayCaseSerializer,
    DiagnosisReplayCaseListSerializer,
    DiagnosisReplayResultSerializer,
    DiagnosisTemplateSerializer,
    DiagnosisTemplateVersionSerializer,
    ObservabilityDataSourceSerializer,
    ObservedServiceSerializer,
    SelfHealingPolicySerializer,
)
from .permissions import AlertWebhookPermission
import logging

logger = logging.getLogger(__name__)


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
        'create_time': ['gte', 'lte'],
    }
    ordering_fields = ['create_time', 'alert_name', 'severity', 'status']

    permission_labels = {
        'bind_healing_pipeline': {'name': '绑定自愈流水线', 'danger': 'warn'},
        'export_to_knowledge': {'name': '导出诊断至知识库', 'danger': 'safe'},
        'trigger_healing': {'name': '触发自愈流水线', 'danger': 'warn'},
        'bulk_destroy': {'name': '批量删除告警', 'danger': 'danger'},
        'report': {'name': '查看告警报表', 'danger': 'safe'},
        'export_report': {'name': '导出告警报表', 'danger': 'safe'},
    }

    @action(detail=False, methods=['post'], url_path='bulk-destroy')
    def bulk_destroy(self, request):
        ids = request.data.get('ids', [])
        if not ids:
            return Response({"error": "No IDs provided"}, status=status.HTTP_400_BAD_REQUEST)
        
        count = self.queryset.filter(id__in=ids).delete()[0]
        return Response({"message": f"Successfully deleted {count} alerts"}, status=status.HTTP_200_OK)

    @action(detail=False, methods=['get'], url_path='report')
    def report(self, request):
        from django.utils.dateparse import parse_datetime, parse_date
        from django.db.models import Count, Q
        from django.db.models.functions import TruncDay
        import datetime
        from django.utils import timezone

        start_str = request.query_params.get('start_time')
        end_str = request.query_params.get('end_time')

        def parse_date_param(param_str, is_end=False):
            if not param_str:
                return None
            try:
                dt = parse_datetime(param_str)
                if dt:
                    if timezone.is_naive(dt):
                        dt = timezone.make_aware(dt)
                    return dt
            except Exception:
                pass
            try:
                d = parse_date(param_str)
                if d:
                    dt = datetime.datetime.combine(d, datetime.time.max if is_end else datetime.time.min)
                    return timezone.make_aware(dt)
            except Exception:
                pass
            return None

        start_time = parse_date_param(start_str, is_end=False)
        end_time = parse_date_param(end_str, is_end=True)

        if not start_time:
            start_time = timezone.now() - datetime.timedelta(days=7)
        if not end_time:
            end_time = timezone.now()

        events = self.filter_queryset(self.get_queryset()).filter(create_time__range=(start_time, end_time))

        # 1. Summary statistics
        total_alerts = events.count()
        firing_alerts = events.filter(status='firing').count()
        resolved_alerts = events.filter(status='resolved').count()
        
        healing_success = events.filter(healing_status='success').count()
        healing_failed = events.filter(healing_status='failed').count()
        healing_executing = events.filter(healing_status='executing').count()
        healing_triggered = healing_success + healing_failed + healing_executing
        
        healing_success_rate = round(healing_success * 100.0 / (healing_success + healing_failed), 2) if (healing_success + healing_failed) > 0 else 0.0

        # 2. Daily trend
        trend_data = events.annotate(day=TruncDay('create_time')) \
                           .values('day') \
                           .annotate(
                               count=Count('id'),
                               resolved=Count('id', filter=Q(status='resolved')),
                               healing=Count('id', filter=Q(healing_status__in=['executing', 'success', 'failed']))
                           ) \
                           .order_by('day')
        
        trend_list = []
        for item in trend_data:
            if item['day']:
                trend_list.append({
                    "date": item['day'].strftime("%Y-%m-%d"),
                    "count": item['count'],
                    "resolved": item['resolved'],
                    "healing": item['healing']
                })

        # 3. Severity Distribution
        severity_data = events.values('severity').annotate(count=Count('id')).order_by('-count')
        severity_list = [{"severity": item['severity'], "count": item['count']} for item in severity_data]

        # 4. Healing Status Distribution
        status_data = events.values('healing_status').annotate(count=Count('id')).order_by('-count')
        status_list = [{"status": item['healing_status'], "count": item['count']} for item in status_data]

        # 5. Grouped by Alert Name for the table
        name_stats = events.values('alert_name', 'severity') \
                           .annotate(
                               count=Count('id'),
                               resolved_count=Count('id', filter=Q(status='resolved')),
                               healing_count=Count('id', filter=Q(healing_status__in=['executing', 'success', 'failed'])),
                               healing_success_count=Count('id', filter=Q(healing_status='success')),
                               healing_failed_count=Count('id', filter=Q(healing_status='failed'))
                           ) \
                           .order_by('-count')
        
        name_list = []
        for item in name_stats:
            total = item['count']
            resolved = item['resolved_count']
            healing = item['healing_count']
            success = item['healing_success_count']
            failed = item['healing_failed_count']

            recovery_rate = round(resolved * 100.0 / total, 2) if total > 0 else 0.0
            healing_success_rate_item = round(success * 100.0 / (success + failed), 2) if (success + failed) > 0 else 0.0

            name_list.append({
                "alert_name": item['alert_name'],
                "severity": item['severity'],
                "count": total,
                "resolved_count": resolved,
                "recovery_rate": recovery_rate,
                "healing_count": healing,
                "healing_success_count": success,
                "healing_failed_count": failed,
                "healing_success_rate": healing_success_rate_item
            })

        return Response({
            "summary": {
                "total_alerts": total_alerts,
                "firing_alerts": firing_alerts,
                "resolved_alerts": resolved_alerts,
                "healing_triggered": healing_triggered,
                "healing_success": healing_success,
                "healing_failed": healing_failed,
                "healing_success_rate": healing_success_rate
            },
            "trend": trend_list,
            "severity_distribution": severity_list,
            "healing_status_distribution": status_list,
            "alerts_by_name": name_list
        }, status=status.HTTP_200_OK)

    @action(detail=False, methods=['post', 'get'], url_path='export-report')
    def export_report(self, request):
        """
        异步流式触发 Celery 导出告警报表
        """
        # 支持 POST body 以及 GET query params 两种传参方式
        start_str = request.data.get('start_time') or request.query_params.get('start_time')
        end_str = request.data.get('end_time') or request.query_params.get('end_time')

        from .tasks import export_alert_report_task
        export_alert_report_task.delay(
            user_id=request.user.id,
            start_time_str=start_str,
            end_time_str=end_str
        )

        return Response({
            "message": "报表正在后台异步生成中，完成后您将在通知中心收到下载提示"
        }, status=status.HTTP_200_OK)


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
            source = self._detect_alert_source(data, labels)

            # 存入数据库
            old_obj = AlertEvent.objects.filter(fingerprint=fingerprint).first()
            old_status = old_obj.status if old_obj else None
            should_analyze = False
            
            if alert_status == 'firing':
                if not old_obj or old_obj.status != 'firing' or old_obj.healing_status in ['none', 'failed']:
                    should_analyze = True

            # 过滤不需要分析的告警名称
            from utils.config_manager import ConfigCache
            ignored_names_str = ConfigCache.get('sre', 'sre.ignored_alert_names', '')
            ignored_names = [name.strip() for name in ignored_names_str.split(',') if name.strip()]
            is_ignored = alert_name in ignored_names
            if is_ignored:
                should_analyze = False

            obj, created = AlertEvent.objects.update_or_create(
                fingerprint=fingerprint,
                defaults={
                    'alert_name': alert_name,
                    'severity': severity,
                    'status': alert_status,
                    'source': source,
                    'labels': labels,
                    'annotations': annotations,
                }
            )

            if is_ignored and obj.healing_status != 'ignored':
                obj.healing_status = 'ignored'
                obj.save(update_fields=['healing_status'])
            
            # 发送警告和恢复通知
            if old_status != 'firing' and alert_status == 'firing':
                try:
                    from apps.system_management.notifiers import notify_alert_firing
                    notify_alert_firing(obj)
                except Exception as ne:
                    logger.error(f"[SRE] Failed to send alert firing notification: {str(ne)}")
            elif old_status == 'firing' and alert_status == 'resolved':
                try:
                    from apps.system_management.notifiers import notify_alert_resolved
                    notify_alert_resolved(obj)
                except Exception as ne:
                    logger.error(f"[SRE] Failed to send alert resolved notification: {str(ne)}")

            # 触发 AI 分析 Celery 任务 (引入 Redis 防抖)
            if should_analyze:
                lock_key = f"alert_analysis_lock_{fingerprint}"
                try:
                    lock_acquired = cache.add(lock_key, True, timeout=300)
                except Exception as ce:
                    logger.warning("[SRE] Alert analysis debounce cache unavailable: %s", ce)
                    lock_acquired = True
                if lock_acquired:
                    obj.healing_status = 'analyzing'
                    obj.save(update_fields=['healing_status'])
                    from .tasks import analyze_alert_event
                    try:
                        analyze_alert_event.delay(obj.id)
                    except Exception as task_exc:
                        logger.warning("[SRE] Failed to enqueue alert analysis task: %s", task_exc)
                        obj.healing_status = 'failed'
                        obj.ai_analysis = f"AI 分析任务提交失败：{task_exc}"
                        obj.save(update_fields=['healing_status', 'ai_analysis'])
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

    def _detect_alert_source(self, payload, labels):
        source = (labels.get('source') or labels.get('datasource') or labels.get('generator') or '').lower()
        if source in {'vmalert', 'victoriametrics'}:
            return source
        generator_url = ''
        alerts = payload.get('alerts') or []
        if alerts and isinstance(alerts[0], dict):
            generator_url = alerts[0].get('generatorURL', '') or ''
        external_url = payload.get('externalURL', '') or ''
        combined = f'{generator_url} {external_url}'.lower()
        if 'vmalert' in combined:
            return 'vmalert'
        if 'victoriametrics' in combined:
            return 'victoriametrics'
        return 'prometheus'

@extend_schema(tags=["SRE 自愈策略"])
class SelfHealingPolicyViewSet(DataScopeMixin, viewsets.ModelViewSet):
    queryset = SelfHealingPolicy.objects.all().order_by('-create_time')
    serializer_class = SelfHealingPolicySerializer
    permission_classes = [SmartRBACPermission]
    resource_code = "sre:policy"
    resource_type = "sre"
    asset_share_type = 'self_healing_policy'
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

    def perform_create(self, serializer):
        serializer.save(project=getattr(self.request, 'project', None))


@extend_schema(tags=["SRE 观测数据源"])
class ObservabilityDataSourceViewSet(viewsets.ModelViewSet):
    queryset = ObservabilityDataSource.objects.all()
    serializer_class = ObservabilityDataSourceSerializer
    permission_classes = [SmartRBACPermission]
    resource_code = "sre:observability"
    resource_type = "sre"
    filterset_fields = {
        'name': ['icontains'],
        'kind': ['exact'],
        'provider': ['exact'],
        'type': ['exact'],
        'is_active': ['exact'],
        'is_default': ['exact'],
    }

    @action(detail=False, methods=['get'], url_path='capabilities')
    def capabilities(self, request):
        return Response(get_datasource_capabilities(), status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='test-connection')
    def test_connection(self, request, pk=None):
        datasource = self.get_object()
        try:
            result = get_observability_adapter(datasource).test_connection()
            return Response(result, status=status.HTTP_200_OK)
        except Exception as exc:
            return Response({'ok': False, 'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    def perform_create(self, serializer):
        instance = serializer.save()
        self._ensure_single_default(instance)

    def perform_update(self, serializer):
        instance = serializer.save()
        self._ensure_single_default(instance)

    def _ensure_single_default(self, instance):
        if instance.is_default:
            ObservabilityDataSource.objects.filter(kind=instance.kind, is_default=True).exclude(id=instance.id).update(is_default=False)


@extend_schema(tags=["SRE 可观测服务"])
class ObservedServiceViewSet(DataScopeMixin, viewsets.ModelViewSet):
    queryset = ObservedService.objects.select_related(
        'project', 'environment', 'resource_pool', 'k8s_cluster', 'metric_datasource', 'log_datasource'
    ).prefetch_related('hosts')
    serializer_class = ObservedServiceSerializer
    permission_classes = [SmartRBACPermission]
    resource_code = "sre:observed-service"
    resource_type = "sre"
    filterset_fields = {
        'name': ['icontains'],
        'code': ['icontains'],
        'project': ['exact'],
        'is_active': ['exact'],
    }

    def get_queryset(self):
        queryset = self.queryset
        project = getattr(self.request, 'project', None)
        if project:
            return queryset.filter(project=project)
        return queryset if self.request.user.is_superuser else queryset.none()

    @action(detail=False, methods=['get'], url_path='match-alert')
    def match_alert(self, request):
        alert_id = request.query_params.get('alert_id')
        project_id = request.query_params.get('project')
        active_project = getattr(request, 'project', None)
        if active_project:
            if project_id not in (None, '') and str(project_id) != str(active_project.id):
                return Response({'project': 'Project must match the active workspace.'}, status=status.HTTP_400_BAD_REQUEST)
            project_id = active_project.id
        if not alert_id:
            return Response({'error': 'alert_id is required'}, status=status.HTTP_400_BAD_REQUEST)
        alert = AlertEvent.objects.filter(id=alert_id).first()
        if not alert:
            return Response({'error': 'Alert not found'}, status=status.HTTP_404_NOT_FOUND)
        result = match_services_for_alert(alert, project_id=project_id)
        return Response(result, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='preview-logs')
    def preview_logs(self, request, pk=None):
        service = self.get_object()
        datasource = service.log_datasource or ObservabilityDataSource.objects.filter(
            kind='log', is_default=True, is_active=True
        ).first()
        if not datasource:
            return Response({'ok': False, 'error': 'No log datasource configured'}, status=status.HTTP_400_BAD_REQUEST)
        start, end = self._preview_time_window(request)
        limit = min(max(int(request.data.get('limit') or 5), 1), 50)
        try:
            result = get_log_adapter(datasource).query_logs(service, start, end, limit=limit)
            items = result.get('items') if isinstance(result, dict) else []
            return Response({
                'ok': True,
                'type': 'logs',
                'service': {'id': service.id, 'name': service.name, 'code': service.code},
                'datasource': self._datasource_summary(datasource),
                'time_range': {'start': start.isoformat(), 'end': end.isoformat()},
                'query': result.get('query') if isinstance(result, dict) else None,
                'count': len(items or []),
                'items': (items or [])[:limit],
                'raw': result.get('result') if isinstance(result, dict) else result,
            }, status=status.HTTP_200_OK)
        except Exception as exc:
            return Response({
                'ok': False,
                'type': 'logs',
                'service': {'id': service.id, 'name': service.name, 'code': service.code},
                'datasource': self._datasource_summary(datasource),
                'time_range': {'start': start.isoformat(), 'end': end.isoformat()},
                'error': str(exc),
            }, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'], url_path='preview-metrics')
    def preview_metrics(self, request, pk=None):
        service = self.get_object()
        datasource = service.metric_datasource or ObservabilityDataSource.objects.filter(
            kind='metric', is_default=True, is_active=True
        ).first()
        if not datasource:
            return Response({'ok': False, 'error': 'No metric datasource configured'}, status=status.HTTP_400_BAD_REQUEST)
        start, end = self._preview_time_window(request)
        step = request.data.get('step') or '60s'
        try:
            metrics = get_metric_adapter(datasource).query_metrics(service, start, end, step=step)
            return Response({
                'ok': True,
                'type': 'metrics',
                'service': {'id': service.id, 'name': service.name, 'code': service.code},
                'datasource': self._datasource_summary(datasource),
                'time_range': {'start': start.isoformat(), 'end': end.isoformat(), 'step': step},
                'count': len(metrics or []),
                'metrics': metrics or [],
            }, status=status.HTTP_200_OK)
        except Exception as exc:
            return Response({
                'ok': False,
                'type': 'metrics',
                'service': {'id': service.id, 'name': service.name, 'code': service.code},
                'datasource': self._datasource_summary(datasource),
                'time_range': {'start': start.isoformat(), 'end': end.isoformat(), 'step': step},
                'error': str(exc),
            }, status=status.HTTP_400_BAD_REQUEST)

    def _preview_time_window(self, request):
        raw_time = request.data.get('diagnosis_time')
        center = parse_datetime(raw_time) if raw_time else None
        if center is None:
            center = timezone.now()
        elif timezone.is_naive(center):
            center = timezone.make_aware(center, timezone.get_current_timezone())
        window_minutes = min(max(int(request.data.get('window_minutes') or 10), 1), 120)
        delta = timedelta(minutes=window_minutes)
        return center - delta, center + delta

    def _datasource_summary(self, datasource):
        return {
            'id': datasource.id,
            'name': datasource.name,
            'kind': datasource.kind,
            'provider': datasource.provider,
            'type': datasource.type,
        }


@extend_schema(tags=["SRE 时间点诊断"])
class DiagnosisRunViewSet(DataScopeMixin, viewsets.ModelViewSet):
    queryset = DiagnosisRun.objects.select_related('project', 'service', 'alert', 'template', 'created_by')
    serializer_class = DiagnosisRunSerializer
    permission_classes = [SmartRBACPermission]
    resource_code = "sre:diagnosis"
    resource_type = "sre"
    resource_owner_field = "created_by"
    filterset_fields = {
        'status': ['exact'],
        'trigger_type': ['exact'],
        'project': ['exact'],
        'service': ['exact'],
        'alert': ['exact'],
        'diagnosis_time': ['gte', 'lte'],
    }

    def get_queryset(self):
        queryset = self.queryset
        project = getattr(self.request, 'project', None)
        if project:
            queryset = queryset.filter(project=project)
        elif not self.request.user.is_superuser:
            queryset = queryset.none()
        if self.action == 'list':
            queryset = queryset.defer('query_params', 'context_snapshot', 'ai_result')
        return queryset

    def get_serializer_class(self):
        if self.action == 'list':
            return DiagnosisRunListSerializer
        return DiagnosisRunSerializer

    def create(self, request, *args, **kwargs):
        data = request.data.copy()
        error = self._prepare_template_diagnosis_payload(data)
        if error:
            return error
        self._prepared_diagnosis_payload = data
        serializer = self.get_serializer(data=data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        headers = self.get_success_headers(serializer.data)
        return Response(serializer.data, status=status.HTTP_201_CREATED, headers=headers)

    @action(detail=False, methods=['post'], url_path='preview')
    def preview(self, request):
        data = request.data.copy()
        error = self._prepare_template_diagnosis_payload(data)
        if error:
            return error
        return Response(self._build_collection_plan(data), status=status.HTTP_200_OK)

    def _build_collection_plan(self, data):
        from .tasks import _select_log_datasources, _select_metric_datasources, _template_collection_config
        warnings = []
        template = DiagnosisTemplate.objects.filter(id=data.get('template')).first() if data.get('template') else None
        template_snapshot = template.to_snapshot() if template else None
        collection = _template_collection_config(template_snapshot)
        service = ObservedService.objects.filter(id=data.get('service')).first() if data.get('service') else None
        alert = AlertEvent.objects.filter(id=data.get('alert')).first() if data.get('alert') else None
        service_match = None
        if alert and not service:
            service_match = match_services_for_alert(alert, project_id=data.get('project') or None)
            best_match = service_match.get('best_match')
            if best_match:
                service = ObservedService.objects.filter(id=best_match['id']).first()
            else:
                warnings.extend(service_match.get('warnings') or ['未匹配到可观测服务，将跳过服务日志和指标采集。'])

        start, end = self._preview_time_window_from_payload(data)
        collect_metrics = collection.get('metrics', True)
        collect_service_logs = collection.get('service_logs', True)
        metric_datasources = []
        log_datasources = []
        metric_skipped_reason = None
        log_skipped_reason = None
        if not collect_metrics:
            metric_skipped_reason = '当前模板未启用指标采集。'
        if not collect_service_logs:
            log_skipped_reason = '当前模板未启用服务日志采集。'

        if service:
            if collect_metrics:
                metric_datasources = _select_metric_datasources(service, template_snapshot)
                if not metric_datasources:
                    warnings.append('未找到可用指标数据源，将跳过指标采集。')
            if collect_service_logs:
                log_datasources = _select_log_datasources(service, template_snapshot)
                if not log_datasources:
                    warnings.append('未找到可用日志数据源，将跳过日志采集。')
        else:
            warnings.append('未选择可观测服务，将跳过服务日志和指标采集。')

        ci_cd_items = []
        ci_cd_labels = {
            'pipeline_run': '流水线运行摘要',
            'failed_nodes': '失败节点',
            'node_logs': '节点日志',
            'approval_records': '审批记录',
            'related_alerts': '关联告警',
            'ansible_execution': 'Ansible 执行摘要',
            'ansible_task_logs': 'Ansible TaskLog',
        }
        for key, label in ci_cd_labels.items():
            enabled = collection.get(key, False)
            ci_cd_items.append({'key': key, 'label': label, 'enabled': bool(enabled)})

        return {
            'ok': True,
            'template': {
                'id': template.id,
                'scope': template.scope,
                'code': template.code,
                'name': template.name,
                'project': template.project_id,
                'target_type': (template.content or {}).get('target_type'),
            } if template else None,
            'target': {
                'project': data.get('project'),
                'pipeline_run_id': data.get('pipeline_run_id'),
                'pipeline_node_run_id': data.get('pipeline_node_run_id'),
                'ansible_execution_id': data.get('ansible_execution_id'),
                'host_id': data.get('host_id'),
                'k8s_cluster_id': data.get('k8s_cluster_id'),
                'namespace': data.get('namespace'),
                'workload_kind': data.get('workload_kind'),
                'workload_name': data.get('workload_name'),
                'jvm_instance': data.get('jvm_instance'),
            },
            'time_range': {'start': start.isoformat(), 'end': end.isoformat()},
            'service': {
                'id': service.id,
                'name': service.name,
                'code': service.code,
                'project': service.project_id,
            } if service else None,
            'service_match': service_match,
            'collection': {
                'metrics': {
                    'enabled': bool(collect_metrics),
                    'datasources': [self._datasource_summary(item) for item in metric_datasources],
                    'skipped_reason': metric_skipped_reason,
                },
                'logs': {
                    'enabled': bool(collect_service_logs),
                    'datasources': [self._datasource_summary(item) for item in log_datasources],
                    'skipped_reason': log_skipped_reason,
                },
                'ci_cd_context': ci_cd_items,
                'ansflow_events': {'enabled': True, 'items': ['alerts', 'pipeline_runs', 'ansible_executions', 'approval_tickets']},
            },
            'warnings': warnings,
        }

    def _preview_time_window_from_payload(self, data):
        raw_time = data.get('diagnosis_time')
        center = parse_datetime(raw_time) if raw_time else None
        if center is None:
            center = timezone.now()
        elif timezone.is_naive(center):
            center = timezone.make_aware(center, timezone.get_current_timezone())
        window_minutes = min(max(int(data.get('window_minutes') or 10), 1), 120)
        delta = timedelta(minutes=window_minutes)
        return center - delta, center + delta

    def _datasource_summary(self, datasource):
        return {
            'id': datasource.id,
            'name': datasource.name,
            'kind': datasource.kind,
            'provider': datasource.provider,
            'type': datasource.type,
        }

    def _prepare_template_diagnosis_payload(self, data):
        from apps.host_management.models import Host
        from apps.k8s_management.models import K8sCluster
        from apps.pipeline_management.models import PipelineNodeRun, PipelineRun
        from apps.task_management.models import AnsibleExecution

        pipeline_run = None
        node_run = None
        request_project = getattr(self.request, 'project', None)
        requested_project_id = data.get('project')
        if request_project:
            if requested_project_id not in (None, '') and str(requested_project_id) != str(request_project.id):
                return Response({'project': 'Project must match the active workspace.'}, status=status.HTTP_400_BAD_REQUEST)
            data['project'] = request_project.id
        pipeline_run_id = data.get('pipeline_run_id')
        pipeline_node_run_id = data.get('pipeline_node_run_id')

        if pipeline_node_run_id not in (None, ''):
            node_run = PipelineNodeRun.objects.select_related('run', 'run__pipeline').filter(id=pipeline_node_run_id).first()
            if not node_run:
                return Response({'pipeline_node_run_id': 'Pipeline node run not found.'}, status=status.HTTP_400_BAD_REQUEST)
            if pipeline_run_id not in (None, '') and str(node_run.run_id) != str(pipeline_run_id):
                return Response({'pipeline_node_run_id': 'Pipeline node run does not belong to pipeline_run_id.'}, status=status.HTTP_400_BAD_REQUEST)
            pipeline_run = node_run.run
            data['pipeline_run_id'] = node_run.run_id
            if data.get('ansible_execution_id') in (None, '') and node_run.node_type in ('ansible', 'host_deploy'):
                ansible_execution_id = self._extract_ansible_execution_id(node_run.output_data)
                if ansible_execution_id not in (None, ''):
                    data['ansible_execution_id'] = ansible_execution_id
        elif pipeline_run_id not in (None, ''):
            pipeline_run = PipelineRun.objects.select_related('pipeline').filter(id=pipeline_run_id).first()
            if not pipeline_run:
                return Response({'pipeline_run_id': 'Pipeline run not found.'}, status=status.HTTP_400_BAD_REQUEST)

        if pipeline_run and getattr(pipeline_run.pipeline, 'project_id', None):
            project_id = pipeline_run.pipeline.project_id
            if data.get('project') in (None, ''):
                data['project'] = project_id
            elif str(data.get('project')) != str(project_id):
                return Response({'project': 'Project does not match the pipeline run project.'}, status=status.HTTP_400_BAD_REQUEST)

        ansible_execution_id = data.get('ansible_execution_id')
        if ansible_execution_id not in (None, ''):
            execution = AnsibleExecution.objects.select_related('task').filter(id=ansible_execution_id).first()
            if not execution:
                return Response({'ansible_execution_id': 'Ansible execution not found.'}, status=status.HTTP_400_BAD_REQUEST)
            execution_project_id = execution.task.project_id
            if data.get('project') not in (None, '') and str(execution_project_id) != str(data.get('project')):
                return Response(
                    {'ansible_execution_id': 'Ansible execution does not belong to the diagnosis project.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        template_code = data.get('template_code')
        if template_code and not data.get('template'):
            project_id = data.get('project')
            template = None
            if project_id not in (None, ''):
                template = DiagnosisTemplate.objects.filter(
                    scope='project',
                    project_id=project_id,
                    code=template_code,
                    is_active=True,
                ).first()
            if not template:
                template = DiagnosisTemplate.objects.filter(
                    scope='global',
                    code=template_code,
                    is_active=True,
                ).first()
            if not template:
                return Response({'template_code': 'Active diagnosis template not found.'}, status=status.HTTP_400_BAD_REQUEST)
            data['template'] = template.id

        template_id = data.get('template')
        template = DiagnosisTemplate.objects.filter(id=template_id, is_active=True).first() if template_id else None
        if template_id and not template:
            return Response({'template': 'Active diagnosis template not found.'}, status=status.HTTP_400_BAD_REQUEST)
        if template and template.scope == 'project' and str(template.project_id) != str(data.get('project')):
            return Response({'template': 'Project template does not belong to the diagnosis project.'}, status=status.HTTP_400_BAD_REQUEST)

        service_id = data.get('service')
        if service_id not in (None, ''):
            service = ObservedService.objects.filter(id=service_id, is_active=True).first()
            if not service:
                return Response({'service': 'Active observed service not found.'}, status=status.HTTP_400_BAD_REQUEST)
            if str(service.project_id) != str(data.get('project')):
                return Response({'service': 'Observed service does not belong to the diagnosis project.'}, status=status.HTTP_400_BAD_REQUEST)
            if data.get('k8s_cluster_id') in (None, '') and service.k8s_cluster_id:
                data['k8s_cluster_id'] = service.k8s_cluster_id
            if data.get('namespace') in (None, '') and service.namespace:
                data['namespace'] = service.namespace

        host_id = data.get('host_id')
        if host_id not in (None, '') and not Host.objects.filter(
            id=host_id,
            project_id=data.get('project'),
        ).exists():
            return Response(
                {'host_id': 'Host does not belong to the diagnosis project.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        cluster_id = data.get('k8s_cluster_id')
        if cluster_id not in (None, '') and not K8sCluster.objects.filter(
            id=cluster_id,
            project_id=data.get('project'),
        ).exists():
            return Response(
                {'k8s_cluster_id': 'Kubernetes cluster does not belong to the diagnosis project.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        alert_id = data.get('alert')
        if alert_id not in (None, ''):
            alert = AlertEvent.objects.filter(id=alert_id).first()
            if not alert:
                return Response({'alert': 'Alert event not found.'}, status=status.HTTP_400_BAD_REQUEST)
            alert_project = (alert.labels or {}).get('project_id') or (alert.labels or {}).get('project')
            project = getattr(request_project, 'code', None)
            allowed_project_values = {str(data.get('project')), str(project)}
            if alert_project not in (None, '') and str(alert_project) not in allowed_project_values:
                return Response({'alert': 'Alert event does not belong to the diagnosis project.'}, status=status.HTTP_400_BAD_REQUEST)

        target_type = (template.content or {}).get('target_type') if template else None
        required_targets = {
            'pipeline_run': ('pipeline_run_id', pipeline_run_id or data.get('pipeline_run_id')),
            'ansible_execution': ('ansible_execution_id', data.get('ansible_execution_id')),
            'service_regression': ('service', data.get('service')),
            'alert_service': ('service', data.get('service') or data.get('alert')),
            'k8s_workload': ('k8s_cluster_id', data.get('k8s_cluster_id')),
            'host_runtime': ('host_id', data.get('host_id') or data.get('service')),
            'jvm_runtime': ('service', data.get('service')),
        }
        if target_type in required_targets:
            field, value = required_targets[target_type]
            if value in (None, ''):
                return Response({field: f'{field} is required for target_type={target_type}.'}, status=status.HTTP_400_BAD_REQUEST)
        return None

    def _extract_ansible_execution_id(self, output_data):
        if not isinstance(output_data, dict):
            return None
        for key in ('ansible_execution_id', 'ansibleExecutionId', 'execution_id'):
            value = output_data.get(key)
            if value not in (None, ''):
                return value
        return None

    def perform_create(self, serializer):
        alert = serializer.validated_data.get('alert')
        service = serializer.validated_data.get('service')
        project = serializer.validated_data.get('project')
        template = serializer.validated_data.get('template')
        service_match = None
        save_kwargs = {'created_by': self.request.user}
        if alert and not service:
            service_match = match_services_for_alert(alert, project_id=getattr(project, 'id', None))
            best_match = service_match.get('best_match')
            if best_match:
                matched_service = ObservedService.objects.filter(id=best_match['id']).first()
                if matched_service:
                    save_kwargs['service'] = matched_service

        instance = serializer.save(**save_kwargs)
        query_params = dict(instance.query_params or {})
        request_data = getattr(self, '_prepared_diagnosis_payload', self.request.data)
        plan_data = request_data.copy()
        if instance.project_id and plan_data.get('project') in (None, ''):
            plan_data['project'] = instance.project_id
        if instance.service_id:
            plan_data['service'] = instance.service_id
        for key in (
            'pipeline_run_id',
            'pipeline_node_run_id',
            'ansible_execution_id',
            'host_id',
            'k8s_cluster_id',
            'namespace',
            'workload_kind',
            'workload_name',
            'jvm_instance',
        ):
            value = request_data.get(key)
            if value not in (None, ''):
                query_params[key] = value
        if template:
            query_params['template_snapshot'] = template.to_snapshot()
        if service_match is not None:
            query_params['service_match'] = service_match
        query_params['collection_plan'] = self._build_collection_plan(plan_data)
        if query_params != (instance.query_params or {}):
            instance.query_params = query_params
            instance.save(update_fields=['query_params'])
        from .tasks import enqueue_diagnosis_run
        try:
            enqueue_diagnosis_run(instance)
        except Exception as task_exc:
            logger.warning("[SRE Diagnosis] Failed to enqueue diagnosis task: %s", task_exc)
            instance.status = 'failed'
            instance.error_message = f"诊断任务提交失败：{task_exc}"
            instance.save(update_fields=['status', 'error_message'])

    @action(detail=True, methods=['post'], url_path='retry')
    def retry(self, request, pk=None):
        obj = self.get_object()
        if obj.status == 'running':
            return Response(
                {'error': 'A running diagnosis cannot be retried.'},
                status=status.HTTP_409_CONFLICT,
            )
        obj.status = 'pending'
        obj.error_message = None
        obj.trigger_type = 'retry'
        obj.finished_at = None
        obj.celery_task_id = None
        obj.save(update_fields=['status', 'error_message', 'trigger_type', 'finished_at', 'celery_task_id'])
        from .tasks import enqueue_diagnosis_run
        try:
            enqueue_diagnosis_run(obj)
        except Exception as task_exc:
            logger.warning("[SRE Diagnosis] Failed to enqueue diagnosis retry: %s", task_exc)
            obj.status = 'failed'
            obj.error_message = f"诊断任务提交失败：{task_exc}"
            obj.save(update_fields=['status', 'error_message'])
        return Response({'message': 'Diagnosis retry submitted'}, status=status.HTTP_200_OK)

    @action(detail=True, methods=['get', 'post'], url_path='feedback')
    def feedback(self, request, pk=None):
        run = self.get_object()
        if request.method == 'GET':
            feedback = DiagnosisFeedback.objects.filter(run=run, user=request.user).first()
            if not feedback:
                return Response(None, status=status.HTTP_200_OK)
            return Response(DiagnosisFeedbackSerializer(feedback).data)
        feedback = DiagnosisFeedback.objects.filter(run=run, user=request.user).first()
        serializer = DiagnosisFeedbackSerializer(
            feedback,
            data=request.data,
            partial=bool(feedback),
            context={'request': request},
        )
        serializer.is_valid(raise_exception=True)
        serializer.save(run=run, user=request.user)
        return Response(
            serializer.data,
            status=status.HTTP_200_OK if feedback else status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=['post'], url_path='compare')
    def compare(self, request, pk=None):
        current = self.get_object()
        other_id = request.data.get('other_run_id')
        if not other_id:
            return Response({'other_run_id': 'This field is required.'}, status=status.HTTP_400_BAD_REQUEST)
        other = self.get_queryset().filter(id=other_id).first()
        if not other:
            return Response({'other_run_id': 'Diagnosis run not found.'}, status=status.HTTP_404_NOT_FOUND)

        def report(run):
            return (run.context_snapshot or {}).get('structured_report') or {}

        current_report = report(current)
        other_report = report(other)
        current_refs = {item.get('ref') for item in (current.context_snapshot or {}).get('evidence_index') or [] if item.get('ref')}
        other_refs = {item.get('ref') for item in (other.context_snapshot or {}).get('evidence_index') or [] if item.get('ref')}
        return Response({
            'current': DiagnosisRunListSerializer(current).data,
            'other': DiagnosisRunListSerializer(other).data,
            'quality_delta': round((current.quality_score or 0) - (other.quality_score or 0), 2),
            'confidence_delta': round((current.confidence_score or 0) - (other.confidence_score or 0), 4),
            'evidence': {
                'added': sorted(current_refs - other_refs),
                'removed': sorted(other_refs - current_refs),
                'shared': sorted(current_refs & other_refs),
            },
            'reports': {
                'current': current_report,
                'other': other_report,
            },
        })

    @action(detail=True, methods=['post'], url_path='create-replay-case')
    def create_replay_case(self, request, pk=None):
        run = self.get_object()
        payload = request.data.copy()
        payload.setdefault('project', run.project_id)
        payload.setdefault('source_run', run.id)
        payload.setdefault('template', run.template_id)
        payload.setdefault('name', f'{run.title} Replay')
        payload.setdefault('fixture_context', redact_sensitive_data(run.context_snapshot or {}))
        serializer = DiagnosisReplayCaseSerializer(data=payload, context={'request': request})
        serializer.is_valid(raise_exception=True)
        case = serializer.save(created_by=request.user)
        return Response(DiagnosisReplayCaseSerializer(case).data, status=status.HTTP_201_CREATED)


@extend_schema(tags=["SRE 诊断模板"])
class DiagnosisTemplateViewSet(DataScopeMixin, viewsets.ModelViewSet):
    queryset = DiagnosisTemplate.objects.select_related('project')
    serializer_class = DiagnosisTemplateSerializer
    permission_classes = [SmartRBACPermission]
    resource_code = "sre:diagnosis-template"
    resource_type = "sre"
    filterset_fields = {
        'scope': ['exact'],
        'code': ['exact'],
        'category': ['exact'],
        'is_active': ['exact'],
        'is_builtin': ['exact'],
    }

    def get_queryset(self):
        queryset = self.queryset
        active_project = getattr(self.request, 'project', None)
        if active_project:
            queryset = queryset.filter(models.Q(scope='global') | models.Q(project=active_project))
        elif not self.request.user.is_superuser:
            return queryset.none()
        project_id = self.request.query_params.get('project')
        include_inactive = self.request.query_params.get('include_inactive') in ('1', 'true', 'True')
        if not include_inactive:
            queryset = queryset.filter(is_active=True)
        if project_id:
            queryset = queryset.filter(models.Q(scope='global') | models.Q(project_id=project_id))
            project_codes = list(DiagnosisTemplate.objects.filter(project_id=project_id).values_list('code', flat=True))
            if project_codes:
                queryset = queryset.exclude(scope='global', code__in=project_codes)
        return queryset

    def perform_create(self, serializer):
        scope = serializer.validated_data.get('scope', 'global')
        if scope == 'global' and not self.request.user.is_superuser:
            raise PermissionDenied('Only platform administrators can create global diagnosis templates.')
        project = getattr(self.request, 'project', None)
        if scope == 'project':
            template = serializer.save(is_builtin=False, project=project)
        else:
            template = serializer.save(is_builtin=False)
        create_template_version(template, self.request.user, 'Initial version')

    def perform_update(self, serializer):
        template = serializer.instance
        versioned_fields = {'name', 'description', 'category', 'content'}
        has_versioned_change = any(
            field in serializer.validated_data
            and serializer.validated_data[field] != getattr(template, field)
            for field in versioned_fields
        )
        if has_versioned_change:
            serializer.validated_data['version'] = template.version + 1
        template = serializer.save()
        if has_versioned_change:
            create_template_version(
                template,
                self.request.user,
                self.request.data.get('change_summary') or 'Template updated',
            )

    def update(self, request, *args, **kwargs):
        template = self.get_object()
        self._ensure_template_mutable(template)
        if template.is_builtin:
            allowed = {'is_active'}
            if any(key not in allowed for key in request.data.keys()):
                return Response({'error': 'Built-in templates can only be enabled or disabled. Copy them before editing content.'}, status=status.HTTP_400_BAD_REQUEST)
        return super().update(request, *args, **kwargs)

    def partial_update(self, request, *args, **kwargs):
        template = self.get_object()
        self._ensure_template_mutable(template)
        if template.is_builtin:
            allowed = {'is_active'}
            if any(key not in allowed for key in request.data.keys()):
                return Response({'error': 'Built-in templates can only be enabled or disabled. Copy them before editing content.'}, status=status.HTTP_400_BAD_REQUEST)
        return super().partial_update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        template = self.get_object()
        self._ensure_template_mutable(template)
        if template.is_builtin:
            return Response({'error': 'Built-in templates cannot be deleted. Disable or copy them instead.'}, status=status.HTTP_400_BAD_REQUEST)
        return super().destroy(request, *args, **kwargs)

    def _ensure_template_mutable(self, template):
        if template.scope == 'global' and not self.request.user.is_superuser:
            raise PermissionDenied('Only platform administrators can modify global diagnosis templates.')

    @action(detail=True, methods=['post'], url_path='copy')
    def copy(self, request, pk=None):
        source = self.get_object()
        data = {
            'scope': request.data.get('scope') or 'project',
            'project': getattr(getattr(request, 'project', None), 'id', None),
            'code': request.data.get('code') or source.code,
            'name': request.data.get('name') or f"{source.name} Copy",
            'description': request.data.get('description') or source.description,
            'category': source.category,
            'content': source.content,
            'is_active': True,
        }
        serializer = self.get_serializer(data=data)
        serializer.is_valid(raise_exception=True)
        template = serializer.save(is_builtin=False)
        create_template_version(template, request.user, f'Copied from {source.code}')
        return Response(self.get_serializer(template).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['get'], url_path='versions')
    def versions(self, request, pk=None):
        template = self.get_object()
        queryset = template.versions.select_related('created_by').all()
        return Response(DiagnosisTemplateVersionSerializer(queryset, many=True).data)

    @action(detail=True, methods=['post'], url_path='rollback')
    def rollback(self, request, pk=None):
        template = self.get_object()
        self._ensure_template_mutable(template)
        if template.is_builtin:
            return Response(
                {'error': 'Built-in templates cannot be rolled back. Copy them before editing.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        version_number = request.data.get('version')
        version = template.versions.filter(version=version_number).first()
        if not version:
            return Response({'version': 'Template version not found.'}, status=status.HTTP_404_NOT_FOUND)
        with transaction.atomic():
            template.version += 1
            template.name = version.name
            template.description = version.description
            template.category = version.category
            template.content = version.content
            template.save(update_fields=[
                'version', 'name', 'description', 'category', 'content', 'update_time',
            ])
            create_template_version(
                template,
                request.user,
                f'Rolled back from version {version.version}',
            )
        return Response(self.get_serializer(template).data)

    @action(detail=True, methods=['post'], url_path='publish')
    def publish(self, request, pk=None):
        template = self.get_object()
        self._ensure_template_mutable(template)
        template.lifecycle_status = 'published'
        template.is_active = True
        template.save(update_fields=['lifecycle_status', 'is_active', 'update_time'])
        return Response(self.get_serializer(template).data)

    @action(detail=True, methods=['post'], url_path='deprecate')
    def deprecate(self, request, pk=None):
        template = self.get_object()
        self._ensure_template_mutable(template)
        if template.is_builtin:
            return Response(
                {'error': 'Built-in templates can be disabled but not deprecated.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        template.lifecycle_status = 'deprecated'
        template.is_active = False
        template.save(update_fields=['lifecycle_status', 'is_active', 'update_time'])
        return Response(self.get_serializer(template).data)

    @action(detail=True, methods=['post'], url_path='run')
    def run(self, request, pk=None):
        template = self.get_object()
        payload = request.data.copy()
        payload['template'] = template.id
        if not payload.get('title'):
            payload['title'] = template.name
        view = DiagnosisRunViewSet()
        view.request = request
        error = view._prepare_template_diagnosis_payload(payload)
        if error:
            return error
        view._prepared_diagnosis_payload = payload
        serializer = DiagnosisRunSerializer(data=payload, context={'request': request})
        serializer.is_valid(raise_exception=True)
        view.perform_create(serializer)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


@extend_schema(tags=["SRE 诊断回放"])
class DiagnosisReplayCaseViewSet(DataScopeMixin, viewsets.ModelViewSet):
    queryset = DiagnosisReplayCase.objects.select_related(
        'project', 'template', 'source_run', 'created_by',
    ).prefetch_related('results')
    serializer_class = DiagnosisReplayCaseSerializer
    permission_classes = [SmartRBACPermission]
    resource_code = "sre:diagnosis"
    resource_type = "sre"
    resource_owner_field = "created_by"
    filterset_fields = {
        'project': ['exact'],
        'template': ['exact'],
        'is_active': ['exact'],
    }

    def get_serializer_class(self):
        if self.action == 'list':
            return DiagnosisReplayCaseListSerializer
        return DiagnosisReplayCaseSerializer

    def get_queryset(self):
        queryset = self.queryset
        project = getattr(self.request, 'project', None)
        if project:
            return queryset.filter(project=project)
        if not self.request.user.is_superuser:
            return queryset.none()
        return queryset

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

    @action(detail=True, methods=['post'], url_path='run')
    def run(self, request, pk=None):
        case = self.get_object()
        result = DiagnosisReplayResult.objects.create(
            case=case,
            template_version=getattr(case.template, 'version', None),
            executed_by=request.user,
        )
        from .tasks import run_diagnosis_replay
        transaction.on_commit(lambda: run_diagnosis_replay.delay(result.id))
        return Response(
            DiagnosisReplayResultSerializer(result).data,
            status=status.HTTP_202_ACCEPTED,
        )

    @action(detail=True, methods=['get'], url_path='results')
    def results(self, request, pk=None):
        case = self.get_object()
        return Response(DiagnosisReplayResultSerializer(
            case.results.select_related('executed_by').all(),
            many=True,
        ).data)


@extend_schema(tags=["SRE 诊断质量"])
class DiagnosisQualityViewSet(viewsets.ViewSet):
    permission_classes = [SmartRBACPermission]
    resource_code = "sre:diagnosis"
    resource_type = "sre"

    def list(self, request):
        project = getattr(request, 'project', None)
        if not project and not request.user.is_superuser:
            return Response(
                {'error': 'An active project is required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        project_id = request.query_params.get('project') or getattr(project, 'id', None)
        if project and project_id and str(project_id) != str(project.id):
            return Response(
                {'project': 'Project must match the active workspace.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(diagnosis_quality_summary(project_id=project_id))


@extend_schema(tags=["SRE 告警规则模板"])
class AlertRuleTemplateViewSet(viewsets.ViewSet):
    serializer_class = AlertRuleTemplateSerializer
    permission_classes = [SmartRBACPermission]
    resource_code = "sre:alert-rule-template"
    resource_type = "sre"

    @extend_schema(responses=AlertRuleTemplateSerializer(many=True))
    def list(self, request):
        return Response(list_templates(), status=status.HTTP_200_OK)

    @extend_schema(request=AlertRuleTemplateRenderRequestSerializer, responses=AlertRuleTemplateRenderSerializer)
    @action(detail=False, methods=['post'], url_path='render')
    def render(self, request):
        template_id = request.data.get('template_id')
        variables = request.data.get('variables') or {}
        if not template_id:
            return Response({'error': 'template_id is required'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            rendered = render_template(template_id, variables)
        except KeyError:
            return Response({'error': 'Template not found'}, status=status.HTTP_404_NOT_FOUND)
        rendered['alertmanager_webhook_example'] = '/api/v1/sre/alerts/receive/?token=<webhook_token>'
        return Response(rendered, status=status.HTTP_200_OK)
