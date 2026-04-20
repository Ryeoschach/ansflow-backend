from unittest.mock import patch, MagicMock
from django.test import TestCase, override_settings
from django.utils import timezone


class GetNotificationConfigTest(TestCase):
    """测试 get_notification_config 的读取逻辑"""

    @patch('apps.system_management.notifiers.ConfigCache')
    def test_read_from_config_cache_first(self, mock_cache):
        """优先从 ConfigCache 读取"""
        mock_cache.get.return_value = 'https://open.feishu.cn/webhook/abc'
        from apps.system_management.notifiers import get_notification_config
        result = get_notification_config('feishu.webhook_url')
        mock_cache.get.assert_called_once_with('notification', 'feishu.webhook_url')
        self.assertEqual(result, 'https://open.feishu.cn/webhook/abc')

    @patch('apps.system_management.notifiers.ConfigCache')
    @patch('os.getenv')
    def test_fallback_to_env_var(self, mock_getenv, mock_cache):
        """ConfigCache 未命中时回退到环境变量"""
        mock_cache.get.return_value = None
        mock_getenv.return_value = 'https://env.feishu.cn/webhook'
        from apps.system_management.notifiers import get_notification_config
        result = get_notification_config('feishu.webhook_url')
        self.assertEqual(result, 'https://env.feishu.cn/webhook')

    @patch('apps.system_management.notifiers.ConfigCache')
    @patch('os.getenv')
    def test_return_default_when_both_empty(self, mock_getenv, mock_cache):
        """ConfigCache 和环境变量都为空时返回 default"""
        mock_cache.get.return_value = None
        mock_getenv.return_value = None
        from apps.system_management.notifiers import get_notification_config
        result = get_notification_config('feishu.webhook_url', default='http://default')
        self.assertEqual(result, 'http://default')

    @patch('apps.system_management.notifiers.ConfigCache')
    def test_frontend_url_env_fallback(self, mock_cache):
        """frontend_url 从 FRONTEND_URL 环境变量读取"""
        mock_cache.get.return_value = None
        import os
        os.environ['FRONTEND_URL'] = 'https://myapp.com'
        from apps.system_management.notifiers import get_notification_config
        result = get_notification_config('frontend_url')
        self.assertEqual(result, 'https://myapp.com')
        del os.environ['FRONTEND_URL']


class IsNotificationEnabledTest(TestCase):
    """测试 is_notification_enabled 的过滤逻辑"""

    def _call(self, event_type='pipeline_start', config=None):
        if config is None:
            config = {}
        with patch('apps.system_management.notifiers.get_notification_config') as mock:
            def side_effect(key, default=None):
                return config.get(key, default)
            mock.side_effect = side_effect
            from apps.system_management.notifiers import is_notification_enabled
            return is_notification_enabled(event_type)

    def test_enabled_by_default(self):
        """默认启用"""
        self.assertTrue(self._call('pipeline_start', {}))

    def test_disabled_by_total_switch(self):
        """总开关关闭则全部禁用"""
        self.assertFalse(self._call('pipeline_start', {'enabled': False}))

    def test_level_none_disables_all(self):
        """level=none 禁用所有"""
        self.assertFalse(self._call('pipeline_start', {'level': 'none'}))
        self.assertFalse(self._call('pipeline_result', {'level': 'none'}))

    def test_level_error_only_allows_failures(self):
        """level=error_only 只允许 pipeline_result / approval_result"""
        self.assertFalse(self._call('pipeline_start', {'level': 'error_only'}))
        self.assertFalse(self._call('approval_requested', {'level': 'error_only'}))
        self.assertTrue(self._call('pipeline_result', {'level': 'error_only'}))
        self.assertTrue(self._call('approval_result', {'level': 'error_only'}))

    def test_notify_on_whitelist(self):
        """notify_on 白名单过滤"""
        self.assertTrue(self._call('pipeline_start', {'notify_on': ['pipeline_start']}))
        self.assertFalse(self._call('pipeline_result', {'notify_on': ['pipeline_start']}))

    def test_combined_rules(self):
        """组合：总开关开 + error_only + 白名单"""
        config = {
            'enabled': True,
            'level': 'error_only',
            'notify_on': ['pipeline_result'],
        }
        self.assertTrue(self._call('pipeline_result', config))
        self.assertFalse(self._call('pipeline_start', config))


class SendNotificationTest(TestCase):
    """测试 _send_notification 发送逻辑"""

    @patch('apps.system_management.notifiers.FeishuNotifier')
    @patch('apps.system_management.notifiers.DingTalkNotifier')
    @patch('apps.system_management.notifiers.get_notification_config')
    def test_send_to_both_channels(self, mock_config, mock_ding, mock_feishu):
        mock_config.side_effect = lambda k, d=None: {
            'feishu.webhook_url': 'https://feishu.cn/hook',
            'dingtalk.webhook_url': 'https://dingtalk.cn/hook',
            'feishu.enabled': True,
            'dingtalk.enabled': True,
            'enabled': True,
            'level': 'all',
            'notify_on': None,
        }.get(k, d)
        mock_feishu_instance = MagicMock()
        mock_feishu.return_value = mock_feishu_instance
        mock_ding_instance = MagicMock()
        mock_ding.return_value = mock_ding_instance

        from apps.system_management.notifiers import _send_notification
        _send_notification('pipeline_start', 'Test Title', 'Test Content', 'http://example.com')

        mock_feishu.assert_called_once_with('https://feishu.cn/hook')
        mock_ding.assert_called_once_with('https://dingtalk.cn/hook')
        mock_feishu_instance.send.assert_called_once()
        mock_ding_instance.send.assert_called_once()

    @patch('apps.system_management.notifiers.FeishuNotifier')
    @patch('apps.system_management.notifiers.get_notification_config')
    def test_skip_disabled_channel(self, mock_config, mock_feishu):
        mock_config.side_effect = lambda k, d=None: {
            'feishu.webhook_url': 'https://feishu.cn/hook',
            'feishu.enabled': False,
            'dingtalk.enabled': False,
            'enabled': True,
            'level': 'all',
            'notify_on': None,
        }.get(k, d)
        mock_feishu_instance = MagicMock()
        mock_feishu.return_value = mock_feishu_instance

        from apps.system_management.notifiers import _send_notification
        _send_notification('pipeline_start', 'Test', 'Content', None)

        mock_feishu_instance.send.assert_not_called()

    @patch('apps.system_management.notifiers.FeishuNotifier')
    @patch('apps.system_management.notifiers.get_notification_config')
    def test_skip_when_no_webhook(self, mock_config, mock_feishu):
        mock_config.side_effect = lambda k, d=None: {
            'feishu.webhook_url': None,
            'feishu.enabled': True,
            'enabled': True,
            'level': 'all',
            'notify_on': None,
        }.get(k, d)

        from apps.system_management.notifiers import _send_notification
        _send_notification('pipeline_start', 'Test', 'Content', None)

        mock_feishu.assert_not_called()


class NotifyPipelineTest(TestCase):
    """测试流水线通知函数"""

    def _mock_notify(self):
        """patch _send_notification 避免真实发送"""
        return patch('apps.system_management.notifiers._send_notification')

    def _make_run(self, name='Test Pipeline', status='success', user='admin'):
        run = MagicMock()
        run.id = 123
        run.pipeline.name = name
        run.pipeline.name = name
        run.status = status
        run.trigger_user.username = user
        run.end_time = run.start_time = timezone.now()
        return run

    @patch('apps.system_management.notifiers._send_notification')
    def test_notify_pipeline_start(self, mock_send):
        from apps.system_management.notifiers import notify_pipeline_start
        run = self._make_run()
        notify_pipeline_start(run)
        mock_send.assert_called_once()
        args, kwargs = mock_send.call_args
        self.assertEqual(args[0], 'pipeline_start')
        self.assertIn('已开始执行', args[1])

    @patch('apps.system_management.notifiers._send_notification')
    def test_notify_pipeline_result_success(self, mock_send):
        from apps.system_management.notifiers import notify_pipeline_result
        run = self._make_run(status='success')
        notify_pipeline_result(run)
        args, kwargs = mock_send.call_args
        self.assertEqual(args[0], 'pipeline_result')
        self.assertIn('执行成功', args[1])

    @patch('apps.system_management.notifiers._send_notification')
    def test_notify_pipeline_result_failed(self, mock_send):
        from apps.system_management.notifiers import notify_pipeline_result
        run = self._make_run(status='failed')
        notify_pipeline_result(run)
        args, kwargs = mock_send.call_args
        self.assertIn('执行失败', args[1])


class NotificationConfigSubscriberTest(TestCase):
    """测试 NotificationConfigSubscriber"""

    @patch('utils.config_manager.ConfigCache')
    def test_invalidate_on_config_change(self, mock_cache):
        from utils.config_subscribers import NotificationConfigSubscriber
        subscriber = NotificationConfigSubscriber()
        subscriber.on_config_changed('notification', 'feishu.webhook_url', 'new_url')
        mock_cache.invalidate.assert_called_once_with('notification', 'feishu.webhook_url')

    def test_should_handle_only_notification_category(self):
        from utils.config_subscribers import NotificationConfigSubscriber
        subscriber = NotificationConfigSubscriber()
        self.assertTrue(subscriber.should_handle('notification'))
        self.assertFalse(subscriber.should_handle('redis'))
        self.assertFalse(subscriber.should_handle('logging'))
