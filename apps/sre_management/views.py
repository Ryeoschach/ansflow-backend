from datetime import timedelta

from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import AllowAny
from django.core.cache import cache
from django.db import models
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from drf_spectacular.utils import extend_schema
from utils.rbac_permission import SmartRBACPermission, DataScopeMixin
from .models import AlertEvent, DiagnosisRun, DiagnosisTemplate, ObservabilityDataSource, ObservedService, SelfHealingPolicy
from .diagnosis_utils import match_services_for_alert
from .observability import get_datasource_capabilities, get_log_adapter, get_metric_adapter, get_observability_adapter
from .rule_templates import list_templates, render_template
from .serializers import (
    AlertEventSerializer,
    AlertRuleTemplateRenderRequestSerializer,
    AlertRuleTemplateRenderSerializer,
    AlertRuleTemplateSerializer,
    DiagnosisRunSerializer,
    DiagnosisTemplateSerializer,
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

    @action(detail=False, methods=['get'], url_path='match-alert')
    def match_alert(self, request):
        alert_id = request.query_params.get('alert_id')
        project_id = request.query_params.get('project')
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
        from apps.pipeline_management.models import PipelineNodeRun, PipelineRun

        pipeline_run = None
        node_run = None
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
        for key in ('pipeline_run_id', 'pipeline_node_run_id', 'ansible_execution_id'):
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
        from .tasks import run_timepoint_diagnosis
        try:
            run_timepoint_diagnosis.delay(instance.id)
        except Exception as task_exc:
            logger.warning("[SRE Diagnosis] Failed to enqueue diagnosis task: %s", task_exc)
            instance.status = 'failed'
            instance.error_message = f"诊断任务提交失败：{task_exc}"
            instance.save(update_fields=['status', 'error_message'])

    @action(detail=True, methods=['post'], url_path='retry')
    def retry(self, request, pk=None):
        obj = self.get_object()
        obj.status = 'pending'
        obj.error_message = None
        obj.trigger_type = 'retry'
        obj.save(update_fields=['status', 'error_message', 'trigger_type'])
        from .tasks import run_timepoint_diagnosis
        try:
            run_timepoint_diagnosis.delay(obj.id)
        except Exception as task_exc:
            logger.warning("[SRE Diagnosis] Failed to enqueue diagnosis retry: %s", task_exc)
            obj.status = 'failed'
            obj.error_message = f"诊断任务提交失败：{task_exc}"
            obj.save(update_fields=['status', 'error_message'])
        return Response({'message': 'Diagnosis retry submitted'}, status=status.HTTP_200_OK)


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
        queryset = super().get_queryset()
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
        serializer.save(is_builtin=False)

    def update(self, request, *args, **kwargs):
        template = self.get_object()
        if template.is_builtin:
            allowed = {'is_active'}
            if any(key not in allowed for key in request.data.keys()):
                return Response({'error': 'Built-in templates can only be enabled or disabled. Copy them before editing content.'}, status=status.HTTP_400_BAD_REQUEST)
        return super().update(request, *args, **kwargs)

    def partial_update(self, request, *args, **kwargs):
        template = self.get_object()
        if template.is_builtin:
            allowed = {'is_active'}
            if any(key not in allowed for key in request.data.keys()):
                return Response({'error': 'Built-in templates can only be enabled or disabled. Copy them before editing content.'}, status=status.HTTP_400_BAD_REQUEST)
        return super().partial_update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        template = self.get_object()
        if template.is_builtin:
            return Response({'error': 'Built-in templates cannot be deleted. Disable or copy them instead.'}, status=status.HTTP_400_BAD_REQUEST)
        return super().destroy(request, *args, **kwargs)

    @action(detail=True, methods=['post'], url_path='copy')
    def copy(self, request, pk=None):
        source = self.get_object()
        data = {
            'scope': request.data.get('scope') or 'project',
            'project': request.data.get('project') or source.project_id,
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
        return Response(self.get_serializer(template).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'], url_path='run')
    def run(self, request, pk=None):
        template = self.get_object()
        payload = request.data.copy()
        payload['template'] = template.id
        if not payload.get('title'):
            payload['title'] = template.name
        serializer = DiagnosisRunSerializer(data=payload, context={'request': request})
        serializer.is_valid(raise_exception=True)
        view = DiagnosisRunViewSet()
        view.request = request
        view.perform_create(serializer)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


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
