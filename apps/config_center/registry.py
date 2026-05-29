from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ConfigCategoryDefinition:
    name: str
    label: str
    description: str
    module: str
    allow_custom_items: bool = False
    allow_delete: bool = False


@dataclass(frozen=True)
class ConfigItemDefinition:
    category: str
    key: str
    default_value: Any
    value_type: str
    description: str
    module: str
    is_encrypted: bool = False
    allow_delete: bool = False
    allow_key_edit: bool = False


SYSTEM_CATEGORIES = {
    "notification": ConfigCategoryDefinition(
        name="notification",
        label="通知配置",
        description="飞书、钉钉、告警接收鉴权和通知事件白名单配置",
        module="system_management",
    ),
    "sre": ConfigCategoryDefinition(
        name="sre",
        label="SRE 配置",
        description="SRE 告警与自愈相关配置",
        module="sre_management",
    ),
    "system": ConfigCategoryDefinition(
        name="system",
        label="系统配置",
        description="系统保留配置分类",
        module="system",
    ),
}


SYSTEM_ITEMS = {
    ("notification", "enabled"): ConfigItemDefinition(
        category="notification",
        key="enabled",
        default_value=True,
        value_type="bool",
        description="总开关：是否启用通知",
        module="system_management",
    ),
    ("notification", "level"): ConfigItemDefinition(
        category="notification",
        key="level",
        default_value="all",
        value_type="string",
        description="通知级别：all=全部，error_only=仅失败，none=禁用",
        module="system_management",
    ),
    ("notification", "feishu.webhook_url"): ConfigItemDefinition(
        category="notification",
        key="feishu.webhook_url",
        default_value="",
        value_type="string",
        description="飞书机器人 Webhook 地址",
        module="system_management",
    ),
    ("notification", "dingtalk.webhook_url"): ConfigItemDefinition(
        category="notification",
        key="dingtalk.webhook_url",
        default_value="",
        value_type="string",
        description="钉钉机器人 Webhook 地址",
        module="system_management",
    ),
    ("notification", "feishu.enabled"): ConfigItemDefinition(
        category="notification",
        key="feishu.enabled",
        default_value=True,
        value_type="bool",
        description="是否启用飞书通知",
        module="system_management",
    ),
    ("notification", "dingtalk.enabled"): ConfigItemDefinition(
        category="notification",
        key="dingtalk.enabled",
        default_value=True,
        value_type="bool",
        description="是否启用钉钉通知",
        module="system_management",
    ),
    ("notification", "frontend_url"): ConfigItemDefinition(
        category="notification",
        key="frontend_url",
        default_value="http://localhost:3000",
        value_type="string",
        description="前端根地址，用于生成详情页链接",
        module="system_management",
    ),
    ("notification", "notify_on"): ConfigItemDefinition(
        category="notification",
        key="notify_on",
        default_value=[
            "pipeline_start",
            "pipeline_result",
            "approval_requested",
            "approval_result",
            "task_result",
            "alert_firing",
            "alert_resolved",
        ],
        value_type="json",
        description="触发通知的事件类型列表",
        module="system_management",
    ),
    ("notification", "webhook_token"): ConfigItemDefinition(
        category="notification",
        key="webhook_token",
        default_value="",
        value_type="string",
        description="告警接收 Webhook 鉴权 Token（留空表示不启用鉴权）",
        module="sre_management",
    ),
    ("sre", "sre.ignored_alert_names"): ConfigItemDefinition(
        category="sre",
        key="sre.ignored_alert_names",
        default_value="",
        value_type="string",
        description="忽略 AI 分析的告警名称列表（多个名称用半角逗号分隔）",
        module="sre_management",
    ),
}


def get_category_definition(category_name: str) -> ConfigCategoryDefinition | None:
    return SYSTEM_CATEGORIES.get(category_name)


def get_item_definition(category_name: str, key: str) -> ConfigItemDefinition | None:
    return SYSTEM_ITEMS.get((category_name, key))


def is_system_category(category_name: str) -> bool:
    return category_name in SYSTEM_CATEGORIES


def is_registered_item(category_name: str, key: str) -> bool:
    return (category_name, key) in SYSTEM_ITEMS


def iter_category_items(category_name: str):
    for (category, _key), definition in SYSTEM_ITEMS.items():
        if category == category_name:
            yield definition


def iter_registered_items():
    return SYSTEM_ITEMS.values()


def registered_category_names() -> set[str]:
    return set(SYSTEM_CATEGORIES.keys())
