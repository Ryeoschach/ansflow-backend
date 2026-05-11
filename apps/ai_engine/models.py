from django.db import models
from utils.base_model import BaseModel
from utils.encryption import encrypt_string, decrypt_string

class AIProvider(BaseModel):
    PROVIDER_TYPES = (
        ("openai", "OpenAI"),
        ("deepseek", "DeepSeek"),
        ("anthropic", "Anthropic"),
        ("ollama", "Ollama (Local)"),
        ("zhipu", "智谱 AI"),
        ("local", "FastEmbed (Local)"),
        ("other", "Other (OpenAI Compatible)"),
    )
    name = models.CharField(max_length=100, unique=True, verbose_name="供应商名称")
    provider_type = models.CharField(max_length=20, choices=PROVIDER_TYPES, verbose_name="供应商类型")
    base_url = models.URLField(max_length=255, blank=True, null=True, verbose_name="API 地址")
    api_key = models.CharField(max_length=512, blank=True, null=True, verbose_name="API Key")
    is_active = models.BooleanField(default=True, verbose_name="是否启用")

    def save(self, *args, **kwargs):
        # 如果 api_key 发生了变化且不是加密后的格式，则进行加密
        if self.api_key and not self.api_key.startswith('gAAAA'): # Fernet 加密通常以 gAAAA 开始
             self.api_key = encrypt_string(self.api_key)
        super().save(*args, **kwargs)

    def get_decrypted_key(self):
        return decrypt_string(self.api_key) if self.api_key else ""

    class Meta:
        db_table = "ai_provider"
        verbose_name = "AI 供应商"
        verbose_name_plural = verbose_name

class AIModel(BaseModel):
    MODEL_TYPES = (
        ("llm", "分析模型 (LLM)"),
        ("embedding", "向量模型 (Embedding)"),
    )
    provider = models.ForeignKey(AIProvider, related_name="models", on_delete=models.CASCADE)
    name = models.CharField(max_length=100, verbose_name="模型标识 (如 gpt-4)")
    display_name = models.CharField(max_length=100, verbose_name="显示名称")
    model_type = models.CharField(max_length=20, choices=MODEL_TYPES, verbose_name="模型类型")
    is_active = models.BooleanField(default=True, verbose_name="是否启用")

    class Meta:
        db_table = "ai_model"
        verbose_name = "AI 模型"
        verbose_name_plural = verbose_name
        unique_together = ('provider', 'name')

class AIConfig(BaseModel):
    name = models.CharField(max_length=100, default="default", unique=True)
    default_llm = models.ForeignKey(AIModel, related_name="default_as_llm", on_delete=models.SET_NULL, null=True, blank=True)
    default_embedding = models.ForeignKey(AIModel, related_name="default_as_embedding", on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        db_table = "ai_config"
        verbose_name = "AI 全局配置"
        verbose_name_plural = verbose_name

class KnowledgeBase(BaseModel):
    name = models.CharField(max_length=255, verbose_name="知识库名称")
    name_en = models.CharField(max_length=255, blank=True, null=True, verbose_name="知识库英文名称")
    description = models.TextField(blank=True, null=True, verbose_name="描述")
    description_en = models.TextField(blank=True, null=True, verbose_name="英文描述")
    collection_name = models.CharField(max_length=255, unique=True, verbose_name="向量集合名称")

    class Meta:
        db_table = "ai_knowledge_base"
        verbose_name = "知识库"
        verbose_name_plural = verbose_name

class KnowledgeDocument(BaseModel):
    SOURCE_TYPES = (
        ("manual", "手动录入"),
        ("file", "文件上传"),
        ("ai_export", "AI 导出"),
    )
    kb = models.ForeignKey(KnowledgeBase, related_name="documents", on_delete=models.CASCADE, verbose_name="所属知识库")
    title = models.CharField(max_length=255, verbose_name="标题")
    content = models.TextField(verbose_name="正文内容")
    source_type = models.CharField(max_length=20, choices=SOURCE_TYPES, default="manual", verbose_name="来源类型")
    metadata = models.JSONField(default=dict, blank=True, verbose_name="元数据")

    class Meta:
        db_table = "ai_knowledge_document"
        verbose_name = "知识文档"
        verbose_name_plural = verbose_name

class AIChatHistory(BaseModel):
    HISTORY_TYPES = (
        ("chat", "Chat"),
        ("diagnose", "Diagnose"),
    )
    user_id = models.CharField(max_length=255, verbose_name="用户ID")
    session_id = models.CharField(max_length=255, unique=True, verbose_name="会话ID")
    title = models.CharField(max_length=255, verbose_name="对话标题", blank=True)
    history_type = models.CharField(
        max_length=20, 
        choices=HISTORY_TYPES, 
        default="chat", 
        verbose_name="会话类型"
    )
    personality = models.CharField(
        max_length=50, 
        choices=(("professional", "Professional"), ("concise", "Concise"), ("humorous", "Humorous")), 
        default="professional", 
        verbose_name="助手性格"
    )

    class Meta:
        db_table = "ai_chat_history"
        verbose_name = "AI 对话历史"
        verbose_name_plural = verbose_name

class AIChatMessage(BaseModel):
    history = models.ForeignKey(AIChatHistory, related_name="messages", on_delete=models.CASCADE)
    role = models.CharField(max_length=50, choices=(("user", "User"), ("assistant", "Assistant"), ("system", "System")), verbose_name="角色")
    content = models.TextField(verbose_name="内容")
    is_exported = models.BooleanField(default=False, verbose_name="是否已导出至知识库")

    class Meta:
        db_table = "ai_chat_message"
        verbose_name = "AI 对话消息"
        verbose_name_plural = verbose_name
