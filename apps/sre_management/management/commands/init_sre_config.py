from django.core.management.base import BaseCommand
from apps.config_center.models import ConfigCategory, ConfigItem


class Command(BaseCommand):
    help = '初始化 SRE 配置（sre 分类）到配置中心'

    def handle(self, *args, **options):
        # 创建 sre 分类
        category, created = ConfigCategory.objects.get_or_create(
            name='sre',
            defaults={
                'label': 'SRE 配置',
                'description': 'SRE 告警与自愈相关配置'
            }
        )
        if created:
            self.stdout.write(self.style.SUCCESS('Created category: sre'))
        else:
            self.stdout.write('Category "sre" already exists')

        # 初始化配置项
        items = [
            ('sre.ignored_alert_names', '', 'string', False, '忽略 AI 分析的告警名称列表（多个名称用半角逗号分隔）'),
        ]

        for key, value, value_type, is_encrypted, description in items:
            item, created = ConfigItem.objects.get_or_create(
                category=category,
                key=key,
                defaults={
                    'value': value,
                    'value_type': value_type,
                    'is_encrypted': is_encrypted,
                    'description': description,
                }
            )
            if created:
                self.stdout.write(self.style.SUCCESS(f'  Created: sre.{key}'))
            else:
                self.stdout.write(f'  Already exists: sre.{key}')

        self.stdout.write(self.style.SUCCESS('\nSRE 配置初始化完成！'))
