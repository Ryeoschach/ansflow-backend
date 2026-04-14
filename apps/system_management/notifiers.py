import requests
import logging
from abc import ABC, abstractmethod
from django.utils import timezone

logger = logging.getLogger(__name__)

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

def notify_pipeline_start(run_obj):
    """
    流水线启动通知
    """
    import os
    logger.info(f"[Notify] 正在尝试发送流水线启动通知: Run #{run_obj.id}")
    feishu_webhook = os.getenv('FEISHU_WEBHOOK')
    dingtalk_webhook = os.getenv('DINGTALK_WEBHOOK')

    title = f"AnsFlow 流水线 {run_obj.pipeline.name} 已开始执行"
    content = (
        f"**Run ID**: #{run_obj.id}\n"
        f"**触发人**: {run_obj.trigger_user.username if run_obj.trigger_user else '系统'}\n"
        f"**启动时间**: {timezone.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    base_frontend_url = os.getenv('FRONTEND_URL', 'http://localhost:3000')
    detail_url = f"{base_frontend_url}/v1/pipeline/runs/{run_obj.id}"

    if feishu_webhook:
        # old_send = FeishuNotifier(feishu_webhook).send
        # 简单起见，只要不报错就完了
        FeishuNotifier(feishu_webhook).send(title, content, detail_url)
    
    if dingtalk_webhook:
        DingTalkNotifier(dingtalk_webhook).send(title, content, detail_url)

def notify_pipeline_result(run_obj):
    """
    统一分发流水线执行结果通知
    """
    # Todo：画个饼吧 配置中心
    import os
    logger.info(f"[Notify] 正在尝试发送流水线执行结果通知: Run #{run_obj.id}, Status: {run_obj.status}")
    feishu_webhook = os.getenv('FEISHU_WEBHOOK')
    dingtalk_webhook = os.getenv('DINGTALK_WEBHOOK')

    title = f"AnsFlow 流水线 {run_obj.pipeline.name} {'执行成功' if run_obj.status == 'success' else '执行失败'}"
    content = (
        f"**Run ID**: #{run_obj.id}\n"
        f"**触发人**: {run_obj.trigger_user.username if run_obj.trigger_user else '系统'}\n"
        f"**最终状态**: {run_obj.status}\n"
        f"**耗时**: {int((run_obj.end_time - run_obj.start_time).total_seconds())}s"
    )
    # 构造前端详情页链接
    # Todo: 连接需要配置化，已处理怎加了FRONTEND_URL， 没有就是默认localhost
    base_frontend_url = os.getenv('FRONTEND_URL', 'http://localhost:3000')
    detail_url = f"{base_frontend_url}/v1/pipeline/runs/{run_obj.id}"

    if feishu_webhook:
        FeishuNotifier(feishu_webhook).send(title, content, detail_url)
    
    if dingtalk_webhook:
        DingTalkNotifier(dingtalk_webhook).send(title, content, detail_url)

def notify_approval_requested(ticket):
    """
    当操作命中了安全策略被拦截挂起时，立即通知相关主管进行 Payload 审查。
    """
    import os
    feishu_webhook = os.getenv('FEISHU_WEBHOOK')
    dingtalk_webhook = os.getenv('DINGTALK_WEBHOOK')
    base_frontend_url = os.getenv('FRONTEND_URL', 'http://localhost:3000')
    detail_url = f"{base_frontend_url}/v1/system/approvals"

    title = "发现高危操作拦截 - 等待审批签名"
    content = (
        f"**申请单号**: #APP-{ticket.id}\n"
        f"**操作事项**: {ticket.title}\n"
        f"**拦截资源**: {ticket.resource_type}\n"
        f"**操作人员**: {ticket.submitter.username}\n"
        f"**拦截时间**: {timezone.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"\n请点击下方按钮进入审批中心进行 Payload 穿透审查。"
    )

    if feishu_webhook:
        FeishuNotifier(feishu_webhook).send(title, content, detail_url)
    if dingtalk_webhook:
        DingTalkNotifier(dingtalk_webhook).send(title, content, detail_url)

def notify_approval_result(ticket):
    """
    当有审核权限的人通过后，反馈给申请人及其周知群组。
    """
    import os
    feishu_webhook = os.getenv('FEISHU_WEBHOOK')
    dingtalk_webhook = os.getenv('DINGTALK_WEBHOOK')
    base_frontend_url = os.getenv('FRONTEND_URL', 'http://localhost:3000')
    # 结果如果是失败/驳回，通知到列表页，如果是运行中，用户可以在历史页看到
    detail_url = f"{base_frontend_url}/v1/system/approvals"

    status_text = "已准予放行" if ticket.status in ['approved', 'finished'] else "已驳回该请求"
    title = f"审批回执: {status_text}"
    content = (
        f"**申请单号**: #APP-{ticket.id}\n"
        f"**操作事项**: {ticket.title}\n"
        f"**申请人**: {ticket.submitter.username}\n"
        f"**审批人**: {ticket.approver.username if ticket.approver else '系统'}\n"
        f"**批复备注**: {ticket.remark or '无'}\n"
    )

    if feishu_webhook:
        FeishuNotifier(feishu_webhook).send(title, content, detail_url)
    if dingtalk_webhook:
        DingTalkNotifier(dingtalk_webhook).send(title, content, detail_url)
