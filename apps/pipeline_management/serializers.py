from rest_framework import serializers
from .models import Pipeline, PipelineRun, PipelineNodeRun, CIEnvironment

class PipelineSerializer(serializers.ModelSerializer):
    creator_name = serializers.CharField(source='creator.username', read_only=True)

    class Meta:
        model = Pipeline
        fields = ['id', 'name', 'desc', 'graph_data', 'creator', 'creator_name', 'is_active', 'timeout', 'cron_expression', 'is_cron_enabled', 'celery_periodic_task_id', 'create_time', 'update_time']
        read_only_fields = ['creator', 'celery_periodic_task_id', 'create_time', 'update_time']

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
