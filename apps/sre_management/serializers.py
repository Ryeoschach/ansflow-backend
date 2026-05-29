from rest_framework import serializers
from .models import AlertEvent, SelfHealingPolicy

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
