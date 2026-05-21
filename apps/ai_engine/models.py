from django.db import models
from utils.base_model import BaseModel
from utils.encryption import encrypt_string, decrypt_string

class AIProvider(BaseModel):
    PROVIDER_TYPES = (
        ("openai", "OpenAI"),
        ("deepseek", "DeepSeek"),
        ("anthropic", "Anthropic"),
        ("ollama", "Ollama (Local)"),
        ("lmstudio", "LM Studio (Local)"),
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
        ("rerank", "重排序模型 (Rerank)"),
        ("vision", "视觉模型 (Vision/OCR)"),
    )
    provider = models.ForeignKey(AIProvider, related_name="models", on_delete=models.CASCADE)
    name = models.CharField(max_length=100, verbose_name="模型标识 (如 gpt-4)")
    display_name = models.CharField(max_length=100, verbose_name="显示名称")
    model_type = models.CharField(max_length=20, choices=MODEL_TYPES, verbose_name="模型类型 (自动衍生)", blank=True)
    capabilities = models.JSONField(default=list, blank=True, verbose_name="模型能力 (多选)")
    num_ctx = models.IntegerField(default=4096, verbose_name="上下文窗口长度 (tokens)")
    is_active = models.BooleanField(default=True, verbose_name="是否启用")

    def save(self, *args, **kwargs):
        # 自动将第一个能力设置为 model_type，保持兼容性
        if self.capabilities and isinstance(self.capabilities, list) and len(self.capabilities) > 0:
            self.model_type = self.capabilities[0]
        elif self.model_type and not self.capabilities:
            self.capabilities = [self.model_type]
        super().save(*args, **kwargs)

    class Meta:
        db_table = "ai_model"
        verbose_name = "AI 模型"
        verbose_name_plural = verbose_name
        unique_together = ('provider', 'name')

class AIConfig(BaseModel):
    name = models.CharField(max_length=100, default="default", unique=True)
    default_llm = models.ForeignKey(AIModel, related_name="default_as_llm", on_delete=models.SET_NULL, null=True, blank=True)
    default_embedding = models.ForeignKey(AIModel, related_name="default_as_embedding", on_delete=models.SET_NULL, null=True, blank=True)
    default_vision = models.ForeignKey(AIModel, related_name="default_as_vision", on_delete=models.SET_NULL, null=True, blank=True, verbose_name="默认视觉/OCR模型")
    default_rerank = models.ForeignKey(AIModel, related_name="default_as_rerank", on_delete=models.SET_NULL, null=True, blank=True, verbose_name="默认重排序模型")
    default_kb = models.ForeignKey('KnowledgeBase', on_delete=models.SET_NULL, null=True, blank=True, verbose_name="默认知识库")
    
    # RAG 参数调优
    rag_top_k = models.IntegerField(default=5, verbose_name="检索召回数量 (Top-K)")
    rag_score_threshold = models.FloatField(default=0.4, verbose_name="相似度阈值")
    rag_vector_weight = models.FloatField(default=0.7, verbose_name="向量搜索权重")
    rag_bm25_weight = models.FloatField(default=0.3, verbose_name="关键词搜索权重")

    class Meta:
        db_table = "ai_config"
        verbose_name = "AI 全局配置"
        verbose_name_plural = verbose_name

class KnowledgeBase(BaseModel):
    REINDEX_STATUS = (
        ("idle", "空闲"),
        ("processing", "重建中"),
        ("success", "重建成功"),
        ("error", "重建异常"),
    )
    name = models.CharField(max_length=255, verbose_name="知识库名称")
    name_en = models.CharField(max_length=255, blank=True, null=True, verbose_name="知识库英文名称")
    description = models.TextField(blank=True, null=True, verbose_name="描述")
    description_en = models.TextField(blank=True, null=True, verbose_name="英文描述")
    collection_name = models.CharField(max_length=255, unique=True, verbose_name="向量集合名称")
    
    # 重建状态记录
    reindex_status = models.CharField(max_length=20, choices=REINDEX_STATUS, default="idle", verbose_name="重建状态")
    last_reindex_at = models.DateTimeField(null=True, blank=True, verbose_name="最近重建时间")
    reindex_error = models.TextField(blank=True, null=True, verbose_name="重建错误信息")

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
    STATUS_CHOICES = (
        ("pending", "待处理"),
        ("parsing", "正在解析"),
        ("cleaning", "清洗中"),
        ("chunking", "正在切片"),
        ("indexing", "正在索引"),
        ("ready", "已就绪"),
        ("error", "异常"),
    )
    PARSER_TYPES = (
        ("auto", "自动识别"),
        ("native", "原生提取 (快速文本)"),
        ("ocr", "OCR 视觉解析"),
        ("hybrid", "混合增强 (带图表文档)"),
    )
    kb = models.ForeignKey(KnowledgeBase, related_name="documents", on_delete=models.CASCADE, verbose_name="所属知识库")
    title = models.CharField(max_length=255, verbose_name="标题")
    content = models.TextField(verbose_name="正文内容")
    file_path = models.CharField(max_length=512, blank=True, null=True, verbose_name="文件存储路径")
    file_type = models.CharField(max_length=100, blank=True, null=True, verbose_name="文件类型")
    source_type = models.CharField(max_length=20, choices=SOURCE_TYPES, default="manual", verbose_name="来源类型")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="ready", verbose_name="处理状态")
    parser_type = models.CharField(max_length=20, choices=PARSER_TYPES, default="auto", verbose_name="解析器类型")
    parsing_prompt = models.TextField(blank=True, null=True, verbose_name="解析提示词")
    chunk_count = models.IntegerField(default=0, verbose_name="切片数量")
    metadata = models.JSONField(default=dict, blank=True, verbose_name="元数据")

    class Meta:
        db_table = "ai_knowledge_document"
        verbose_name = "知识文档"
        verbose_name_plural = verbose_name

class KnowledgeChunk(BaseModel):
    document = models.ForeignKey(KnowledgeDocument, related_name="chunks", on_delete=models.CASCADE, verbose_name="所属文档")
    content = models.TextField(verbose_name="分块内容")
    vector_id = models.CharField(max_length=100, db_index=True, verbose_name="向量库ID")
    index = models.IntegerField(default=0, verbose_name="序号")
    is_active = models.BooleanField(default=True, verbose_name="是否启用")
    metadata = models.JSONField(default=dict, blank=True, verbose_name="元数据")

    class Meta:
        db_table = "ai_knowledge_chunk"
        verbose_name = "知识分块"
        verbose_name_plural = verbose_name
        ordering = ['document', 'index']

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
    metadata = models.JSONField(default=dict, blank=True, verbose_name="元数据")
    is_exported = models.BooleanField(default=False, verbose_name="是否已导出至知识库")

    class Meta:
        db_table = "ai_chat_message"
        verbose_name = "AI 对话消息"
        verbose_name_plural = verbose_name
