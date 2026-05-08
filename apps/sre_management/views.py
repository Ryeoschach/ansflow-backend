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

@extend_schema(tags=["SRE 自愈策略"])
class SelfHealingPolicyViewSet(viewsets.ModelViewSet):
    queryset = SelfHealingPolicy.objects.all()
    serializer_class = SelfHealingPolicySerializer
    permission_classes = [SmartRBACPermission]
    resource_code = "sre:policy"
    resource_type = "sre"
