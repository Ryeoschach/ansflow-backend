from rest_framework import serializers
from .models import KnowledgeBase, AIChatHistory, AIChatMessage, AIProvider, AIModel, AIConfig, KnowledgeDocument

class AIProviderSerializer(serializers.ModelSerializer):
    class Meta:
        model = AIProvider
        fields = ["id", "name", "provider_type", "base_url", "api_key", "is_active", "create_time"]
        extra_kwargs = {
            'api_key': {'write_only': True}
        }

class AIModelSerializer(serializers.ModelSerializer):
    provider_name = serializers.ReadOnlyField(source='provider.name')
    class Meta:
        model = AIModel
        fields = ["id", "provider", "provider_name", "name", "display_name", "model_type", "is_active", "create_time"]

class AIConfigSerializer(serializers.ModelSerializer):
    class Meta:
        model = AIConfig
        fields = ["id", "name", "default_llm", "default_embedding", "update_time"]

class KnowledgeBaseSerializer(serializers.ModelSerializer):
    class Meta:
        model = KnowledgeBase
        fields = "__all__"

class KnowledgeDocumentSerializer(serializers.ModelSerializer):
    class Meta:
        model = KnowledgeDocument
        fields = ["id", "kb", "title", "content", "source_type", "status", "chunk_count", "metadata", "create_time"]

class AIChatMessageSerializer(serializers.ModelSerializer):
    class Meta:
        model = AIChatMessage
        fields = ["id", "role", "content", "is_exported", "create_time"]

class AIChatHistorySerializer(serializers.ModelSerializer):
    class Meta:
        model = AIChatHistory
        fields = "__all__"
