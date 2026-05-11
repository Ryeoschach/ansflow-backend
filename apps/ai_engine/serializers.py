from rest_framework import serializers
from .models import KnowledgeBase, AIChatHistory, AIChatMessage

class KnowledgeBaseSerializer(serializers.ModelSerializer):
    class Meta:
        model = KnowledgeBase
        fields = "__all__"

class AIChatMessageSerializer(serializers.ModelSerializer):
    class Meta:
        model = AIChatMessage
        fields = ["id", "role", "content", "is_exported", "create_time"]

class AIChatHistorySerializer(serializers.ModelSerializer):
    class Meta:
        model = AIChatHistory
        fields = "__all__"
