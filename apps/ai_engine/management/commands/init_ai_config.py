import os
from django.core.management.base import BaseCommand
from apps.ai_engine.models import AIProvider, AIModel, AIConfig, KnowledgeBase
from django.db import transaction

class Command(BaseCommand):
    help = '初始化 AI 供应商和模型配置'

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS('开始初始化 AI 配置...'))

        with transaction.atomic():
            # 1. 初始化 DeepSeek 供应商
            ds_base_url = os.environ.get("LLM_API_BASE", "https://api.deepseek.com")
            ds_api_key = os.environ.get("LLM_API_KEY", "")

            ds_provider, created = AIProvider.objects.update_or_create(
                name="DeepSeek 官方",
                defaults={
                    "provider_type": "deepseek",
                    "base_url": ds_base_url,
                    "api_key": ds_api_key,
                    "is_active": True
                }
            )
            if created:
                self.stdout.write(f"创建供应商: {ds_provider.name}")

            # 2. 初始化 DeepSeek 模型
            ds_model, created = AIModel.objects.update_or_create(
                provider=ds_provider,
                name="deepseek-chat",
                defaults={
                    "display_name": "DeepSeek Chat V3",
                    "model_type": "llm",
                    "is_active": True
                }
            )
            if created:
                self.stdout.write(f"创建模型: {ds_model.display_name}")

            # 3. 初始化本地 Embedding 供应商
            local_provider, created = AIProvider.objects.update_or_create(
                name="本地模型 (FastEmbed)",
                defaults={
                    "provider_type": "local",
                    "base_url": "http://localhost",
                    "api_key": "none",
                    "is_active": True
                }
            )

            # 4. 初始化 BAAI Embedding 模型
            emb_model, created = AIModel.objects.update_or_create(
                provider=local_provider,
                name="BAAI/bge-small-en-v1.5",
                defaults={
                    "display_name": "BGE Small (Local)",
                    "model_type": "embedding",
                    "is_active": True
                }
            )
            if created:
                self.stdout.write(f"创建模型: {emb_model.display_name}")

            # 5. 初始化默认知识库
            kb, created = KnowledgeBase.objects.update_or_create(
                collection_name="ansflow_docs",
                defaults={
                    "name": "默认运维知识库",
                    "description": "存储平台默认的运维手册、故障诊断经验和自动化脚本说明。"
                }
            )
            if created:
                self.stdout.write(f"创建知识库: {kb.name}")

            # 6. 设置全局默认配置
            config, created = AIConfig.objects.update_or_create(
                name="default",
                defaults={
                    "default_llm": ds_model,
                    "default_embedding": emb_model
                }
            )
            self.stdout.write(self.style.SUCCESS('成功设置全局默认 AI 配置'))

        self.stdout.write(self.style.SUCCESS('AI 配置初始化完成！'))
