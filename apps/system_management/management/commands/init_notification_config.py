"""
初始化通知配置到配置中心
"""
from django.core.management.base import BaseCommand
from apps.config_center.models import ConfigCategory, ConfigItem
from apps.config_center.registry import SYSTEM_CATEGORIES, iter_category_items


class Command(BaseCommand):
    help = '初始化通知配置（notification 分类）到配置中心'

    def handle(self, *args, **options):
        definition = SYSTEM_CATEGORIES['notification']

        # 创建 notification 分类
        category, created = ConfigCategory.objects.get_or_create(
            name=definition.name,
            defaults={
                'label': definition.label,
                'description': definition.description
            }
        )
        if created:
            self.stdout.write(self.style.SUCCESS('Created category: notification'))
        else:
            self.stdout.write('Category "notification" already exists')

        for item_definition in iter_category_items('notification'):
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
                self.stdout.write(self.style.SUCCESS(f'  Created: notification.{item_definition.key}'))
            else:
                if item_definition.key == 'notify_on':
                    current_list = item.value
                    if not isinstance(current_list, list):
                        current_list = []
                    
                    updated = False
                    for new_event in ['alert_firing', 'alert_resolved']:
                        if new_event not in current_list:
                            current_list.append(new_event)
                            updated = True
                    
                    if updated:
                        item.value = current_list
                        item.save(update_fields=['value'])
                        self.stdout.write(self.style.SUCCESS(f'  Updated notification.{item_definition.key} with new event types.'))

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
                    self.stdout.write(self.style.SUCCESS(f'  Synced metadata: notification.{item_definition.key}'))
                self.stdout.write(f'  Already exists: notification.{item_definition.key}')

        self.stdout.write(self.style.SUCCESS('\n通知配置初始化完成！'))
        self.stdout.write('配置路径: /api/v1/config/categories/ (找到 notification 分类)')
