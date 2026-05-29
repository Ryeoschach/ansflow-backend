from django.core.management.base import BaseCommand
from apps.config_center.models import ConfigCategory, ConfigItem
from apps.config_center.registry import SYSTEM_CATEGORIES, iter_category_items


class Command(BaseCommand):
    help = '初始化 SRE 配置（sre 分类）到配置中心'

    def handle(self, *args, **options):
        definition = SYSTEM_CATEGORIES['sre']

        # 创建 sre 分类
        category, created = ConfigCategory.objects.get_or_create(
            name=definition.name,
            defaults={
                'label': definition.label,
                'description': definition.description
            }
        )
        if created:
            self.stdout.write(self.style.SUCCESS('Created category: sre'))
        else:
            self.stdout.write('Category "sre" already exists')

        for item_definition in iter_category_items('sre'):
            item, created = ConfigItem.objects.get_or_create(
                category=category,
                key=item_definition.key,
                defaults={
                    'value': item_definition.default_value,
                    'value_type': item_definition.value_type,
                    'is_encrypted': item_definition.is_encrypted,
                    'description': item_definition.description,
                }
            )
            if created:
                self.stdout.write(self.style.SUCCESS(f'  Created: sre.{item_definition.key}'))
            else:
                changed_fields = []
                if item.value_type != item_definition.value_type:
                    item.value_type = item_definition.value_type
                    changed_fields.append('value_type')
                if item.is_encrypted != item_definition.is_encrypted:
                    item.is_encrypted = item_definition.is_encrypted
                    changed_fields.append('is_encrypted')
                if item.description != item_definition.description:
                    item.description = item_definition.description
                    changed_fields.append('description')
                if changed_fields:
                    item.save(update_fields=changed_fields)
                    self.stdout.write(self.style.SUCCESS(f'  Synced metadata: sre.{item_definition.key}'))
                self.stdout.write(f'  Already exists: sre.{item_definition.key}')

        self.stdout.write(self.style.SUCCESS('\nSRE 配置初始化完成！'))
