from rest_framework import serializers
from .models import WorkerNode, TaskPulse

class WorkerNodeSerializer(serializers.ModelSerializer):
    class Meta:
        model = WorkerNode
        fields = '__all__'

class TaskPulseSerializer(serializers.ModelSerializer):
    worker_name = serializers.CharField(source='worker.hostname', read_only=True)
    
    class Meta:
        model = TaskPulse
        fields = '__all__'
