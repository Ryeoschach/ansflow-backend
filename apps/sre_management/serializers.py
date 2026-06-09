from rest_framework import serializers
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
from .diagnosis_security import merge_redacted_secrets, redact_sensitive_data
from .observability import validate_observability_url

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
        if 'query_config' in attrs and self.instance:
            attrs['query_config'] = merge_redacted_secrets(
                attrs['query_config'],
                self.instance.query_config or {},
            )
        base_url = attrs.get('base_url', getattr(self.instance, 'base_url', None))
        if base_url:
            validate_observability_url(base_url)
        return attrs

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data['query_config'] = redact_sensitive_data(data.get('query_config') or {})
        return data


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

    def validate(self, attrs):
        request = self.context.get('request')
        request_project = getattr(request, 'project', None) if request else None
        project = attrs.get('project', getattr(self.instance, 'project', None))
        if request_project:
            if project and project.id != request_project.id:
                raise serializers.ValidationError({'project': 'Project must match the active workspace.'})
            attrs['project'] = request_project
        return attrs


class DiagnosisTemplateSerializer(serializers.ModelSerializer):
    project_name = serializers.CharField(source='project.name', read_only=True)

    class Meta:
        model = DiagnosisTemplate
        fields = '__all__'
        read_only_fields = ['is_builtin', 'version']

    def validate(self, attrs):
        scope = attrs.get('scope', getattr(self.instance, 'scope', 'global'))
        project = attrs.get('project', getattr(self.instance, 'project', None))
        request = self.context.get('request')
        request_project = getattr(request, 'project', None) if request else None
        if scope == 'project' and request_project:
            if project and project.id != request_project.id:
                raise serializers.ValidationError({'project': 'Project must match the active workspace.'})
            attrs['project'] = request_project
            project = request_project
        code = attrs.get('code', getattr(self.instance, 'code', None))
        content = attrs.get('content', getattr(self.instance, 'content', {}) or {})
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
        if not isinstance(content, dict):
            raise serializers.ValidationError({'content': 'Template content must be an object.'})
        target_type = content.get('target_type')
        allowed_target_types = {
            'pipeline_run',
            'ansible_execution',
            'service_regression',
            'alert_service',
            'k8s_workload',
            'host_runtime',
            'jvm_runtime',
        }
        if target_type not in allowed_target_types:
            raise serializers.ValidationError({'content': 'Template target_type is required and must be valid.'})
        context_collection = content.get('context_collection', {})
        if context_collection and not isinstance(context_collection, dict):
            raise serializers.ValidationError({'content': 'Template context_collection must be an object.'})
        prompt_template = content.get('prompt_template')
        if not isinstance(prompt_template, str) or '{diagnosis_context}' not in prompt_template:
            raise serializers.ValidationError({'content': 'Template prompt_template must include {diagnosis_context}.'})
        log_keywords = content.get('log_keywords', [])
        if log_keywords and not isinstance(log_keywords, list):
            raise serializers.ValidationError({'content': 'Template log_keywords must be a list.'})
        report_schema = content.get('report_schema', {})
        if report_schema and not isinstance(report_schema, dict):
            raise serializers.ValidationError({'content': 'Template report_schema must be an object.'})
        metric_datasource_ids = content.get('metric_datasource_ids', [])
        if metric_datasource_ids:
            if not isinstance(metric_datasource_ids, list):
                raise serializers.ValidationError({'content': 'Template metric_datasource_ids must be a list.'})
            try:
                normalized_metric_datasource_ids = [int(item) for item in metric_datasource_ids]
            except (TypeError, ValueError):
                raise serializers.ValidationError({'content': 'Template metric_datasource_ids must contain numeric datasource ids.'})
            active_metric_datasource_count = ObservabilityDataSource.objects.filter(
                id__in=normalized_metric_datasource_ids,
                kind='metric',
                is_active=True,
            ).count()
            if active_metric_datasource_count != len(set(normalized_metric_datasource_ids)):
                raise serializers.ValidationError({'content': 'Template metric_datasource_ids must reference active metric datasources.'})
            content['metric_datasource_ids'] = normalized_metric_datasource_ids
        log_datasource_ids = content.get('log_datasource_ids', [])
        if log_datasource_ids:
            if not isinstance(log_datasource_ids, list):
                raise serializers.ValidationError({'content': 'Template log_datasource_ids must be a list.'})
            try:
                normalized_log_datasource_ids = [int(item) for item in log_datasource_ids]
            except (TypeError, ValueError):
                raise serializers.ValidationError({'content': 'Template log_datasource_ids must contain numeric datasource ids.'})
            active_log_datasource_count = ObservabilityDataSource.objects.filter(
                id__in=normalized_log_datasource_ids,
                kind='log',
                is_active=True,
            ).count()
            if active_log_datasource_count != len(set(normalized_log_datasource_ids)):
                raise serializers.ValidationError({'content': 'Template log_datasource_ids must reference active log datasources.'})
            content['log_datasource_ids'] = normalized_log_datasource_ids
        return attrs


class DiagnosisTemplateVersionSerializer(serializers.ModelSerializer):
    created_by_username = serializers.CharField(source='created_by.username', read_only=True)

    class Meta:
        model = DiagnosisTemplateVersion
        fields = '__all__'
        read_only_fields = ['template', 'version', 'created_by']


class DiagnosisFeedbackSerializer(serializers.ModelSerializer):
    user_username = serializers.CharField(source='user.username', read_only=True)

    class Meta:
        model = DiagnosisFeedback
        fields = '__all__'
        read_only_fields = ['run', 'user']

    def validate(self, attrs):
        for field in ('accuracy_rating', 'evidence_rating', 'actionability_rating'):
            value = attrs.get(field, getattr(self.instance, field, None))
            if value is None or not 1 <= int(value) <= 5:
                raise serializers.ValidationError({field: 'Rating must be between 1 and 5.'})
        return attrs


class DiagnosisReplayResultSerializer(serializers.ModelSerializer):
    executed_by_username = serializers.CharField(source='executed_by.username', read_only=True)

    class Meta:
        model = DiagnosisReplayResult
        fields = '__all__'
        read_only_fields = [
            'case', 'template_version', 'status', 'score', 'passed',
            'structured_report', 'ai_result', 'evaluation', 'error_message',
            'started_at', 'finished_at', 'executed_by',
        ]


class DiagnosisReplayCaseSerializer(serializers.ModelSerializer):
    project_name = serializers.CharField(source='project.name', read_only=True)
    template_name = serializers.CharField(source='template.name', read_only=True)
    source_run_title = serializers.CharField(source='source_run.title', read_only=True)
    created_by_username = serializers.CharField(source='created_by.username', read_only=True)
    latest_result = serializers.SerializerMethodField()

    class Meta:
        model = DiagnosisReplayCase
        fields = '__all__'
        read_only_fields = ['created_by']

    def get_latest_result(self, obj):
        result = obj.results.order_by('-create_time').first()
        return DiagnosisReplayResultSerializer(result).data if result else None

    def validate(self, attrs):
        request = self.context.get('request')
        request_project = getattr(request, 'project', None) if request else None
        project = attrs.get('project', getattr(self.instance, 'project', None))
        source_run = attrs.get('source_run', getattr(self.instance, 'source_run', None))
        template = attrs.get('template', getattr(self.instance, 'template', None))
        if request_project:
            if project and project.id != request_project.id:
                raise serializers.ValidationError({'project': 'Project must match the active workspace.'})
            attrs['project'] = request_project
            project = request_project
        if source_run and project and source_run.project_id != project.id:
            raise serializers.ValidationError({'source_run': 'Source run does not belong to the replay project.'})
        if template and template.scope == 'project' and project and template.project_id != project.id:
            raise serializers.ValidationError({'template': 'Template does not belong to the replay project.'})
        fixture_context = attrs.get('fixture_context', getattr(self.instance, 'fixture_context', None))
        if not fixture_context and source_run:
            attrs['fixture_context'] = redact_sensitive_data(source_run.context_snapshot or {})
            fixture_context = attrs['fixture_context']
        if not fixture_context:
            raise serializers.ValidationError({'fixture_context': 'Replay context or source_run is required.'})
        expected = attrs.get('expected', getattr(self.instance, 'expected', None)) or {}
        if source_run and not expected:
            report = (source_run.context_snapshot or {}).get('structured_report') or {}
            causes = report.get('possible_causes') or []
            attrs['expected'] = {
                'root_cause_keywords': [
                    item.get('title')
                    for item in causes
                    if isinstance(item, dict) and item.get('title')
                ],
                'evidence_refs': sorted({
                    str(ref)
                    for item in causes
                    if isinstance(item, dict)
                    for ref in item.get('evidence_refs') or []
                }),
                'minimum_score': 60,
            }
        return attrs


class DiagnosisReplayCaseListSerializer(DiagnosisReplayCaseSerializer):
    class Meta(DiagnosisReplayCaseSerializer.Meta):
        fields = [
            'id', 'project', 'project_name', 'name', 'description',
            'template', 'template_name', 'source_run', 'source_run_title',
            'is_active', 'created_by', 'created_by_username', 'latest_result',
            'create_time', 'update_time',
        ]


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
            'evidence_coverage', 'confidence_score', 'quality_score',
        ]

    def validate(self, attrs):
        project = attrs.get('project', getattr(self.instance, 'project', None))
        service = attrs.get('service', getattr(self.instance, 'service', None))
        template = attrs.get('template', getattr(self.instance, 'template', None))
        request = self.context.get('request')
        request_project = getattr(request, 'project', None) if request else None

        if request_project and project and project.id != request_project.id:
            raise serializers.ValidationError({'project': 'Project must match the active workspace.'})
        if request_project and not project:
            attrs['project'] = request_project
            project = request_project
        if service and project and service.project_id != project.id:
            raise serializers.ValidationError({'service': 'Observed service does not belong to the diagnosis project.'})
        if template and template.scope == 'project':
            if not project or template.project_id != project.id:
                raise serializers.ValidationError({'template': 'Project template does not belong to the diagnosis project.'})
        window_minutes = attrs.get('window_minutes', getattr(self.instance, 'window_minutes', 10))
        if not 1 <= window_minutes <= 120:
            raise serializers.ValidationError({'window_minutes': 'Window minutes must be between 1 and 120.'})
        return attrs


class DiagnosisRunListSerializer(serializers.ModelSerializer):
    project_name = serializers.CharField(source='project.name', read_only=True)
    service_name = serializers.CharField(source='service.name', read_only=True)
    alert_name = serializers.CharField(source='alert.alert_name', read_only=True)
    template_name = serializers.CharField(source='template.name', read_only=True)
    template_code = serializers.CharField(source='template.code', read_only=True)
    created_by_username = serializers.CharField(source='created_by.username', read_only=True)

    class Meta:
        model = DiagnosisRun
        fields = [
            'id', 'title', 'project', 'project_name', 'service', 'service_name',
            'alert', 'alert_name', 'template', 'template_name', 'template_code',
            'trigger_type', 'status', 'diagnosis_time', 'window_minutes',
            'error_message', 'created_by_username', 'started_at', 'finished_at',
            'celery_task_id', 'attempt_count', 'heartbeat_at',
            'evidence_coverage', 'confidence_score', 'quality_score',
            'create_time', 'update_time',
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
