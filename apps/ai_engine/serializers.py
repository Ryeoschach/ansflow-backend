import re
from rest_framework import serializers
from .models import (
    KnowledgeBase, AIChatHistory, AIChatMessage, 
    AIProvider, AIModel, AIConfig, KnowledgeDocument, KnowledgeChunk, AIPromptTemplate
)

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
        fields = ["id", "provider", "provider_name", "name", "display_name", "model_type", "capabilities", "num_ctx", "is_active", "create_time"]

class AIConfigSerializer(serializers.ModelSerializer):
    class Meta:
        model = AIConfig
        fields = [
            "id", "name", "default_llm", "default_embedding", "default_vision", "default_rerank", "default_kb",
            "rag_top_k", "rag_score_threshold", "rag_vector_weight", "rag_bm25_weight",
            "update_time"
        ]

class KnowledgeBaseSerializer(serializers.ModelSerializer):
    class Meta:
        model = KnowledgeBase
        fields = "__all__"

class KnowledgeDocumentSerializer(serializers.ModelSerializer):
    class Meta:
        model = KnowledgeDocument
        fields = ["id", "kb", "title", "content", "source_type", "status", "parser_type", "parsing_prompt", "chunk_count", "metadata", "create_time"]

class KnowledgeChunkSerializer(serializers.ModelSerializer):
    class Meta:
        model = KnowledgeChunk
        fields = ["id", "document", "content", "index", "is_active", "metadata", "create_time"]

class AIChatMessageSerializer(serializers.ModelSerializer):
    referenced_docs = serializers.SerializerMethodField()

    class Meta:
        model = AIChatMessage
        fields = ["id", "role", "content", "is_exported", "referenced_docs", "create_time"]

    def get_referenced_docs(self, obj):
        # 从 metadata 中提取参考文档
        if isinstance(obj.metadata, dict):
            return obj.metadata.get('referenced_docs', [])
        return []

class AIChatHistorySerializer(serializers.ModelSerializer):
    class Meta:
        model = AIChatHistory
        fields = "__all__"

class AIPromptTemplateSerializer(serializers.ModelSerializer):
    class Meta:
        model = AIPromptTemplate
        fields = ["id", "name", "code", "template", "description", "is_system", "create_time", "update_time"]
        read_only_fields = ["code", "is_system", "create_time", "update_time"]

    def validate(self, attrs):
        template = attrs.get('template')
        if template is not None:
            code = getattr(self.instance, 'code', None) or attrs.get('code')
            from .prompt_defaults import DEFAULT_PROMPTS
            if code and code in DEFAULT_PROMPTS:
                required = DEFAULT_PROMPTS[code].get("required_variables", [])
                pattern = r'(?<!{){([a-zA-Z_][a-zA-Z0-9_]*?)}(?!})'
                variables = set(re.findall(pattern, template))
                missing = [v for v in required if v not in variables]
                if missing:
                    raise serializers.ValidationError(
                        {"template": f"该提示词模板缺少必需的变量占位符：{', '.join(['{' + m + '}' for m in missing])}。请不要删除这些占位符。"}
                    )
        return attrs
