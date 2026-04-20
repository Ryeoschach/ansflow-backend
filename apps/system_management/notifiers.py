import requests
import logging
import os
from abc import ABC, abstractmethod
from django.utils import timezone
from utils.config_manager import ConfigCache

logger = logging.getLogger(__name__)


def get_notification_config(key: str, default=None):
    """
    从配置中心读取通知配置，优先读取配置中心，
    回退到环境变量（兼容未迁移前的部署）。
    """
    value = ConfigCache.get('notification', key)
    if value is not None:
        return value
    # 环境变量回退
    env_map = {
        'feishu.webhook_url': 'FEISHU_WEBHOOK',
        'dingtalk.webhook_url': 'DINGTALK_WEBHOOK',
        'frontend_url': 'FRONTEND_URL',
        'enabled': None,
        'level': None,
        'feishu.enabled': None,
        'dingtalk.enabled': None,
    }
    env_key = env_map.get(key)
    if env_key:
        env_val = os.getenv(env_key)
        if env_val is not None:
            return env_val
    return default


def is_notification_enabled(event_type: str) -> bool:
    """
    检查通知是否启用。
    event_type: pipeline_start / pipeline_result / approval_requested / approval_result
    """
    # 总开关
    enabled = get_notification_config('enabled', True)
    if not enabled:
        return False

    # 级别过滤
    level = get_notification_config('level', 'all')
    if level == 'none':
        return False
    if level == 'error_only' and event_type not in ('pipeline_result', 'approval_result'):
        return False

    # 事件类型白名单
    notify_on = get_notification_config('notify_on', None)
    if notify_on and event_type not in notify_on:
        return False

    return True


class BaseNotifier(ABC):
    @abstractmethod
    def send(self, title: str, content: str, url: str = None):
        pass


class FeishuNotifier(BaseNotifier):
    """
    飞书机器人告警实现
    """
    def __init__(self, webhook_url):
        self.webhook_url = webhook_url

    def send(self, title: str, content: str, url: str = None):
        if "失败" in title or "错误" in title:
            color = "red"
        elif "开始" in title or "启动" in title:
            color = "blue"
        else:
            color = "green"
        payload = {
            "msg_type": "interactive",
            "card": {
                "config": {"enable_forward": True, "update_multi": True},
                "header": {
                    "template": color,
                    "title": {"content": title, "tag": "plain_text"}
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {"content": f"**内容详情**:\n{content}", "tag": "lark_md"}
                    },
                    {
                        "actions": [
                            {
                                "tag": "button",
                                "text": {"content": "查看详情 (RunViewer)", "tag": "plain_text"},
                                "url": url or "http://localhost:3000",
                                "type": "default"
                            }
                        ],
                        "tag": "action"
                    }
                ]
            }
        }
        try:
            requests.post(self.webhook_url, json=payload, timeout=5)
        except Exception as e:
            logger.error(f"Feishu Notification failed: {str(e)}")


class DingTalkNotifier(BaseNotifier):
    """
    钉钉机器人告警实现
    """
    def __init__(self, webhook_url):
        self.webhook_url = webhook_url

    def send(self, title: str, content: str, url: str = None):
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": title,
                "text": f"### {title}\n\n{content}\n\n[点击查看详情]({url})"
            }
        }
        try:
            requests.post(self.webhook_url, json=payload, timeout=5)
        except Exception as e:
            logger.error(f"DingTalk Notification failed: {str(e)}")


def _send_notification(event_type: str, title: str, content: str, detail_url: str = None):
    """
    统一通知发送入口，读取配置中心的 webhook 配置。
    event_type: pipeline_start / pipeline_result / approval_requested / approval_result
    """
    if not is_notification_enabled(event_type):
        logger.debug(f"Notification disabled for event type: {event_type}")
        return

    feishu_webhook = get_notification_config('feishu.webhook_url')
    dingtalk_webhook = get_notification_config('dingtalk.webhook_url')
    feishu_enabled = get_notification_config('feishu.enabled', True)
    dingtalk_enabled = get_notification_config('dingtalk.enabled', True)

    if feishu_webhook and feishu_enabled:
        FeishuNotifier(feishu_webhook).send(title, content, detail_url)

    if dingtalk_webhook and dingtalk_enabled:
        DingTalkNotifier(dingtalk_webhook).send(title, content, detail_url)


def _get_frontend_url() -> str:
    return get_notification_config('frontend_url', 'http://localhost:3000')


def notify_pipeline_start(run_obj):
    """
    流水线启动通知
    """
    logger.info(f"[Notify] 正在尝试发送流水线启动通知: Run #{run_obj.id}")
    title = f"AnsFlow 流水线 {run_obj.pipeline.name} 已开始执行"
    content = (
        f"**Run ID**: #{run_obj.id}\n"
        f"**触发人**: {run_obj.trigger_user.username if run_obj.trigger_user else '系统'}\n"
        f"**启动时间**: {timezone.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    detail_url = f"{_get_frontend_url()}/v1/pipeline/runs/{run_obj.id}"
    _send_notification('pipeline_start', title, content, detail_url)


def notify_pipeline_result(run_obj):
    """
    统一分发流水线执行结果通知
    """
    logger.info(f"[Notify] 正在尝试发送流水线执行结果通知: Run #{run_obj.id}, Status: {run_obj.status}")
    title = f"AnsFlow 流水线 {run_obj.pipeline.name} {'执行成功' if run_obj.status == 'success' else '执行失败'}"
    content = (
        f"**Run ID**: #{run_obj.id}\n"
        f"**触发人**: {run_obj.trigger_user.username if run_obj.trigger_user else '系统'}\n"
        f"**最终状态**: {run_obj.status}\n"
        f"**耗时**: {int((run_obj.end_time - run_obj.start_time).total_seconds())}s"
    )
    detail_url = f"{_get_frontend_url()}/v1/pipeline/runs/{run_obj.id}"
    _send_notification('pipeline_result', title, content, detail_url)


def notify_approval_requested(ticket):
    """
    当操作命中了安全策略被拦截挂起时，立即通知相关主管进行 Payload 审查。
    """
    title = "发现高危操作拦截 - 等待审批签名"
    content = (
        f"**申请单号**: #APP-{ticket.id}\n"
        f"**操作事项**: {ticket.title}\n"
        f"**拦截资源**: {ticket.resource_type}\n"
        f"**操作人员**: {ticket.submitter.username}\n"
        f"**拦截时间**: {timezone.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"\n请点击下方按钮进入审批中心进行 Payload 穿透审查。"
    )
    detail_url = f"{_get_frontend_url()}/v1/system/approvals"
    _send_notification('approval_requested', title, content, detail_url)


def notify_approval_result(ticket):
    """
    当有审核权限的人通过后，反馈给申请人及其周知群组。
    """
    status_text = "已准予放行" if ticket.status in ['approved', 'finished'] else "已驳回该请求"
    title = f"审批回执: {status_text}"
    content = (
        f"**申请单号**: #APP-{ticket.id}\n"
        f"**操作事项**: {ticket.title}\n"
        f"**申请人**: {ticket.submitter.username}\n"
        f"**审批人**: {ticket.approver.username if ticket.approver else '系统'}\n"
        f"**批复备注**: {ticket.remark or '无'}\n"
    )
    detail_url = f"{_get_frontend_url()}/v1/system/approvals"
    _send_notification('approval_result', title, content, detail_url)
