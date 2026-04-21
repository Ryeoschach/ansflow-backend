from rest_framework import serializers
from .models import Pipeline, PipelineRun, PipelineNodeRun, CIEnvironment, PipelineWebhook, PipelineVersion

class PipelineSerializer(serializers.ModelSerializer):
    creator_name = serializers.CharField(source='creator.username', read_only=True)
    current_version = serializers.SerializerMethodField()

    class Meta:
        model = Pipeline
        fields = ['id', 'name', 'desc', 'graph_data', 'creator', 'creator_name', 'is_active', 'timeout', 'cron_expression', 'is_cron_enabled', 'celery_periodic_task_id', 'create_time', 'update_time', 'current_version']
        read_only_fields = ['creator', 'celery_periodic_task_id', 'create_time', 'update_time']

    def get_current_version(self, obj) -> int | None:
        current = obj.versions.filter(is_current=True).first()
        return current.version_number if current else None


class PipelineVersionSerializer(serializers.ModelSerializer):
    creator_name = serializers.CharField(source='creator.username', read_only=True)
    pipeline_name = serializers.CharField(source='pipeline.name', read_only=True)

    class Meta:
        model = PipelineVersion
        fields = [
            'id', 'pipeline', 'pipeline_name', 'version_number', 'name', 'desc',
            'graph_data', 'creator', 'creator_name', 'change_summary',
            'is_current', 'create_time', 'update_time'
        ]
        read_only_fields = ['id', 'pipeline', 'creator', 'create_time', 'update_time']


class PipelineNodeRunSerializer(serializers.ModelSerializer):
    class Meta:
        model = PipelineNodeRun
        fields = '__all__'

class PipelineRunSerializer(serializers.ModelSerializer):
    trigger_username = serializers.CharField(source='trigger_user.username', read_only=True)
    pipeline_name = serializers.CharField(source='pipeline.name', read_only=True)
    graph_data = serializers.JSONField(source='pipeline.graph_data', read_only=True)
    nodes = PipelineNodeRunSerializer(many=True, read_only=True) # 方便前端一次性取回节点状态进行渲染
    parent_run_id = serializers.IntegerField(source='parent_run.id', read_only=True, allow_null=True)
    skipped_nodes = serializers.SerializerMethodField()

    class Meta:
        model = PipelineRun
        fields = '__all__'
        read_only_fields = ['trigger_user', 'start_time', 'end_time']

    def get_skipped_nodes(self, obj) -> list:
        return list(obj.nodes.filter(status='skipped').values_list('node_id', flat=True))

class CIEnvironmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = CIEnvironment
        fields = '__all__'


class PipelineWebhookSerializer(serializers.ModelSerializer):
    pipeline_name = serializers.CharField(source='pipeline.name', read_only=True)
    webhook_url = serializers.SerializerMethodField()

    class Meta:
        model = PipelineWebhook
        fields = [
            'id', 'pipeline', 'pipeline_name', 'name', 'event_type', 'repository_url',
            'branch_filter', 'secret_key', 'is_active', 'description',
            'last_trigger_time', 'trigger_count', 'webhook_url',
            'create_time', 'update_time'
        ]
        read_only_fields = ['id', 'last_trigger_time', 'trigger_count', 'create_time', 'update_time']

    def get_webhook_url(self, obj) -> str:
        request = self.context.get('request')
        if request:
            base = request.build_absolute_uri('/').rstrip('/')
            return f"{base}/api/v1/pipeline/webhooks/{obj.id}/trigger/"
        return f"/api/v1/pipeline/webhooks/{obj.id}/trigger/"
