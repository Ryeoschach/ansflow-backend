from rest_framework import serializers
from .models import (
    AlertEvent,
    DiagnosisRun,
    DiagnosisTemplate,
    ObservabilityDataSource,
    ObservedService,
    SelfHealingPolicy,
)

class AlertEventSerializer(serializers.ModelSerializer):
    is_auto_execute = serializers.SerializerMethodField()
    matched_policy_name = serializers.SerializerMethodField()
    suggested_pipeline_name = serializers.CharField(source='suggested_pipeline.name', read_only=True)

    class Meta:
        model = AlertEvent
        fields = '__all__'

    def get_is_auto_execute(self, obj):
        # 优先使用数据库中存好的状态 (auto/manual)
        return obj.trigger_type == 'auto'

    def get_matched_policy_name(self, obj):
        # 优先使用数据库中存好的持久化名称
        return obj.matched_policy_name

class SelfHealingPolicySerializer(serializers.ModelSerializer):
    class Meta:
        model = SelfHealingPolicy
        fields = '__all__'


class ObservabilityDataSourceSerializer(serializers.ModelSerializer):
    password = serializers.CharField(required=False, allow_blank=True, allow_null=True, write_only=True)
    token = serializers.CharField(required=False, allow_blank=True, allow_null=True, write_only=True)
    has_password = serializers.SerializerMethodField()
    has_token = serializers.SerializerMethodField()

    class Meta:
        model = ObservabilityDataSource
        fields = [
            'id', 'name', 'kind', 'provider', 'type', 'base_url', 'auth_type', 'username',
            'password', 'token', 'has_password', 'has_token',
            'query_config', 'field_mapping', 'response_mapping',
            'is_default', 'is_active', 'timeout_seconds', 'remark',
            'create_time', 'update_time',
        ]

    def get_has_password(self, obj):
        return bool(obj.password)

    def get_has_token(self, obj):
        return bool(obj.token)

    def validate(self, attrs):
        auth_type = attrs.get('auth_type', getattr(self.instance, 'auth_type', 'none'))
        if auth_type == 'basic' and not attrs.get('username') and not getattr(self.instance, 'username', None):
            raise serializers.ValidationError({'username': 'Basic auth requires username.'})
        provider = attrs.get('provider') or attrs.get('type') or getattr(self.instance, 'provider', None) or getattr(self.instance, 'type', None)
        if provider:
            attrs.setdefault('provider', provider)
            attrs.setdefault('type', provider)
        if not attrs.get('kind'):
            current_kind = getattr(self.instance, 'kind', None)
            attrs['kind'] = current_kind or ('metric' if provider == 'victoriametrics' else 'log')
        return attrs


class ObservedServiceSerializer(serializers.ModelSerializer):
    project_name = serializers.CharField(source='project.name', read_only=True)
    environment_name = serializers.CharField(source='environment.name', read_only=True)
    resource_pool_name = serializers.CharField(source='resource_pool.name', read_only=True)
    k8s_cluster_name = serializers.CharField(source='k8s_cluster.name', read_only=True)
    metric_datasource_name = serializers.CharField(source='metric_datasource.name', read_only=True)
    log_datasource_name = serializers.CharField(source='log_datasource.name', read_only=True)

    class Meta:
        model = ObservedService
        fields = '__all__'


class DiagnosisTemplateSerializer(serializers.ModelSerializer):
    project_name = serializers.CharField(source='project.name', read_only=True)

    class Meta:
        model = DiagnosisTemplate
        fields = '__all__'
        read_only_fields = ['is_builtin']

    def validate(self, attrs):
        scope = attrs.get('scope', getattr(self.instance, 'scope', 'global'))
        project = attrs.get('project', getattr(self.instance, 'project', None))
        code = attrs.get('code', getattr(self.instance, 'code', None))
        if scope == 'project' and not project:
            raise serializers.ValidationError({'project': 'Project template requires project.'})
        if scope == 'global':
            attrs['project'] = None
            project = None
        if code:
            queryset = DiagnosisTemplate.objects.filter(scope=scope, project=project, code=code)
            if self.instance:
                queryset = queryset.exclude(id=self.instance.id)
            if queryset.exists():
                raise serializers.ValidationError({'code': 'Template code already exists in this scope.'})
        return attrs


class DiagnosisRunSerializer(serializers.ModelSerializer):
    project_name = serializers.CharField(source='project.name', read_only=True)
    service_name = serializers.CharField(source='service.name', read_only=True)
    alert_name = serializers.CharField(source='alert.alert_name', read_only=True)
    template_name = serializers.CharField(source='template.name', read_only=True)
    template_code = serializers.CharField(source='template.code', read_only=True)
    created_by_username = serializers.CharField(source='created_by.username', read_only=True)

    class Meta:
        model = DiagnosisRun
        fields = '__all__'
        read_only_fields = [
            'status', 'context_snapshot', 'ai_result', 'error_message',
            'started_at', 'finished_at', 'created_by',
        ]


class AlertRuleTemplateSerializer(serializers.Serializer):
    id = serializers.CharField()
    name = serializers.CharField()
    category = serializers.CharField()
    description = serializers.CharField()
    variables = serializers.JSONField()


class AlertRuleTemplateRenderRequestSerializer(serializers.Serializer):
    template_id = serializers.CharField()
    variables = serializers.JSONField(required=False)


class AlertRuleTemplateRenderSerializer(serializers.Serializer):
    id = serializers.CharField()
    name = serializers.CharField()
    yaml = serializers.CharField()
    variables = serializers.JSONField()
    alertmanager_webhook_example = serializers.CharField()
