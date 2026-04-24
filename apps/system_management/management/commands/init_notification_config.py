"""
初始化通知配置到配置中心
"""
from django.core.management.base import BaseCommand
from apps.config_center.models import ConfigCategory, ConfigItem


class Command(BaseCommand):
    help = '初始化通知配置（notification 分类）到配置中心'

    def handle(self, *args, **options):
        # 创建 notification 分类
        category, created = ConfigCategory.objects.get_or_create(
            name='notification',
            defaults={
                'label': '通知配置',
                'description': '钉钉/飞书等通知渠道的配置'
            }
        )
        if created:
            self.stdout.write(self.style.SUCCESS('Created category: notification'))
        else:
            self.stdout.write('Category "notification" already exists')

        # 初始化配置项
        items = [
            # 基础开关
            ('enabled', True, 'bool', False, '总开关：是否启用通知'),
            ('level', 'all', 'string', False, '通知级别：all=全部, error_only=仅失败, none=禁用'),
            # Webhook URL
            ('feishu.webhook_url', '', 'string', False, '飞书机器人 Webhook 地址'),
            ('dingtalk.webhook_url', '', 'string', False, '钉钉机器人 Webhook 地址'),
            # 渠道开关
            ('feishu.enabled', True, 'bool', False, '是否启用飞书通知'),
            ('dingtalk.enabled', True, 'bool', False, '是否启用钉钉通知'),
            # 前端地址
            ('frontend_url', 'http://localhost:3000', 'string', False, '前端根地址，用于生成详情页链接'),
            # 事件类型白名单
            ('notify_on', ['pipeline_start', 'pipeline_result', 'approval_requested', 'approval_result', 'task_result'], 'json', False, '触发通知的事件类型列表'),
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
                self.stdout.write(self.style.SUCCESS(f'  Created: notification.{key}'))
            else:
                self.stdout.write(f'  Already exists: notification.{key}')

        self.stdout.write(self.style.SUCCESS('\n通知配置初始化完成！'))
        self.stdout.write('配置路径: /api/v1/config/categories/ (找到 notification 分类)')
