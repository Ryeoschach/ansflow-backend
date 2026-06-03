from django.core.management.base import BaseCommand
from django.db import transaction

from apps.ai_engine.models import AIPromptTemplate
from apps.ai_engine.prompt_defaults import DEFAULT_PROMPTS


class Command(BaseCommand):
    help = "同步系统默认 AI 提示词模板，默认只补齐缺失模板，不覆盖已有自定义内容。"

    def add_arguments(self, parser):
        parser.add_argument(
            "--overwrite",
            action="store_true",
            help="覆盖已有系统模板内容。谨慎使用，会重置用户修改过的模板正文。",
        )

    def handle(self, *args, **options):
        overwrite = options["overwrite"]
        created_count = 0
        updated_count = 0
        skipped_count = 0

        with transaction.atomic():
            for code, info in DEFAULT_PROMPTS.items():
                defaults = {
                    "name": info["name"],
                    "description": info.get("description", ""),
                    "template": info["template"],
                    "is_system": True,
                }
                prompt, created = AIPromptTemplate.objects.get_or_create(
                    code=code,
                    defaults=defaults,
                )
                if created:
                    created_count += 1
                    continue

                changed_fields = []
                for field in ("name", "description"):
                    value = defaults[field]
                    if getattr(prompt, field) != value:
                        setattr(prompt, field, value)
                        changed_fields.append(field)

                if not prompt.is_system:
                    prompt.is_system = True
                    changed_fields.append("is_system")

                if overwrite and prompt.template != defaults["template"]:
                    prompt.template = defaults["template"]
                    changed_fields.append("template")

                if changed_fields:
                    prompt.save(update_fields=changed_fields)
                    updated_count += 1
                else:
                    skipped_count += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"AI 提示词同步完成：新增 {created_count}，更新 {updated_count}，跳过 {skipped_count}。"
            )
        )
