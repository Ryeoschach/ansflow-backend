from rest_framework import serializers
from .models import AlertEvent, SelfHealingPolicy

class AlertEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = AlertEvent
        fields = '__all__'

class SelfHealingPolicySerializer(serializers.ModelSerializer):
    class Meta:
        model = SelfHealingPolicy
        fields = '__all__'
