from django.core.management import call_command
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.config_center.models import ConfigCategory, ConfigItem
from apps.config_center.serializers import ConfigItemSerializer


class ConfigRegistryTestCase(TestCase):
    def test_init_commands_create_registered_items_and_preserve_values(self):
        call_command('init_notification_config')
        call_command('init_sre_config')

        notification = ConfigCategory.objects.get(name='notification')
        webhook_token = ConfigItem.objects.get(category=notification, key='webhook_token')
        self.assertEqual(webhook_token.value, '')
        self.assertEqual(webhook_token.value_type, 'string')

        webhook_token.value = 'secret-token'
        webhook_token.description = 'legacy description'
        webhook_token.save(update_fields=['value', 'description'])

        call_command('init_notification_config')
        webhook_token.refresh_from_db()
        self.assertEqual(webhook_token.value, 'secret-token')
        self.assertEqual(webhook_token.description, '告警接收 Webhook 鉴权 Token（留空表示不启用鉴权）')

        sre = ConfigCategory.objects.get(name='sre')
        self.assertTrue(ConfigItem.objects.filter(category=sre, key='sre.ignored_alert_names').exists())

    def test_serializer_exposes_registry_metadata(self):
        call_command('init_notification_config')
        item = ConfigItem.objects.get(category__name='notification', key='webhook_token')

        data = ConfigItemSerializer(item).data

        self.assertTrue(data['registered'])
        self.assertTrue(data['is_system'])
        self.assertEqual(data['config_scope'], 'system')
        self.assertEqual(data['module'], 'sre_management')
        self.assertFalse(data['allow_delete'])

    def test_system_category_rejects_unregistered_item(self):
        call_command('init_sre_config')
        category = ConfigCategory.objects.get(name='sre')

        serializer = ConfigItemSerializer(data={
            'category': category.id,
            'key': 'unknown_key',
            'value': 'x',
            'value_type': 'string',
            'description': 'custom key in system category',
        })

        self.assertFalse(serializer.is_valid())
        self.assertIn('key', serializer.errors)

    def test_registered_item_blocks_identity_field_changes(self):
        call_command('init_notification_config')
        item = ConfigItem.objects.get(category__name='notification', key='webhook_token')

        serializer = ConfigItemSerializer(item, data={'value_type': 'json'}, partial=True)

        self.assertFalse(serializer.is_valid())
        self.assertIn('value_type', serializer.errors)

    def test_registry_endpoint_marks_existing_items(self):
        call_command('init_notification_config')
        user = get_user_model().objects.create_superuser(
            username='registry-admin',
            email='registry-admin@example.com',
            password='test-pass',
        )
        client = APIClient()
        client.force_authenticate(user=user)

        response = client.get('/api/v1/config/items/registry/')

        self.assertEqual(response.status_code, 200)
        items = {
            (item['category'], item['key']): item
            for item in response.data['items']
        }
        self.assertTrue(items[('notification', 'webhook_token')]['exists'])
        self.assertFalse(items[('sre', 'sre.ignored_alert_names')]['exists'])
