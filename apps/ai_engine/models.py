from django.db import models
from utils.base_model import BaseModel

class KnowledgeBase(BaseModel):
    name = models.CharField(max_length=255, verbose_name="知识库名称")
    description = models.TextField(blank=True, null=True, verbose_name="描述")
    collection_name = models.CharField(max_length=255, unique=True, verbose_name="向量集合名称")

    class Meta:
        db_table = "ai_knowledge_base"
        verbose_name = "知识库"
        verbose_name_plural = verbose_name

class AIChatHistory(BaseModel):
    user_id = models.CharField(max_length=255, verbose_name="用户ID")
    session_id = models.CharField(max_length=255, unique=True, verbose_name="会话ID")
    title = models.CharField(max_length=255, verbose_name="对话标题", blank=True)

    class Meta:
        db_table = "ai_chat_history"
        verbose_name = "AI 对话历史"
        verbose_name_plural = verbose_name

class AIChatMessage(BaseModel):
    history = models.ForeignKey(AIChatHistory, related_name="messages", on_delete=models.CASCADE)
    role = models.CharField(max_length=50, choices=(("user", "User"), ("assistant", "Assistant"), ("system", "System")), verbose_name="角色")
    content = models.TextField(verbose_name="内容")

    class Meta:
        db_table = "ai_chat_message"
        verbose_name = "AI 对话消息"
        verbose_name_plural = verbose_name
