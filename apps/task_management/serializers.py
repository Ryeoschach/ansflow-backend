from rest_framework import serializers
from apps.task_management.models import AnsibleTask, AnsibleExecution, TaskLog


class TaskLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = TaskLog
        fields = '__all__'


class AnsibleExecutionSerializer(serializers.ModelSerializer):
    task_name = serializers.CharField(source='task.name', read_only=True)
    executor_name = serializers.CharField(source='executor.username', read_only=True)
    task_type = serializers.CharField(source='task.task_type', read_only=True)
    resource_pool_name = serializers.CharField(source='task.resource_pool.name', read_only=True)

    class Meta:
        model = AnsibleExecution
        fields = [
            'id', 'task', 'task_name', 'task_type', 'resource_pool_name', 'status', 
            'executor', 'executor_name', 'result_summary', 'celery_task_id', 'start_time', 'end_time', 'create_time'
        ]
        read_only_fields = ['status', 'result_summary', 'create_time', 'start_time', 'end_time']


class AnsibleTaskSerializer(serializers.ModelSerializer):
    resource_pool_name = serializers.CharField(source='resource_pool.name', read_only=True)
    creator_name = serializers.CharField(source='creator.username', read_only=True)
    last_execution_status = serializers.SerializerMethodField()

    class Meta:
        model = AnsibleTask
        fields = [
            'id', 'name', 'task_type', 'resource_pool', 'resource_pool_name',
            'content', 'extra_vars', 'timeout', 'creator', 'creator_name', 'create_time', 
            'update_time', 'last_execution_status'
        ]
        read_only_fields = ['creator', 'create_time', 'update_time']

    def get_last_execution_status(self, obj):
        last = obj.executions.first()
        if last:
            return last.status
        return None
