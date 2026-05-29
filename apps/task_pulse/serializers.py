from rest_framework import serializers
from .models import WorkerNode, TaskPulse

class WorkerNodeSerializer(serializers.ModelSerializer):
    class Meta:
        model = WorkerNode
        fields = '__all__'

class TaskPulseSerializer(serializers.ModelSerializer):
    worker_name = serializers.SerializerMethodField()

    def get_worker_name(self, obj):
        return obj.worker_hostname or (obj.worker.hostname if obj.worker else None)
    
    class Meta:
        model = TaskPulse
        fields = '__all__'
