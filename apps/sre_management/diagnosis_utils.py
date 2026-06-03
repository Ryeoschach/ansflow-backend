from __future__ import annotations

import re
import json
from typing import Any

from .models import AlertEvent, ObservedService


SERVICE_MATCH_THRESHOLD = 60
SERVICE_LABEL_KEYS = ('service', 'app', 'application', 'job', 'component')
NAMESPACE_LABEL_KEYS = ('namespace', 'kubernetes_namespace', 'ns')
LOG_HIGHLIGHT_KEYWORDS = (
    'error',
    'exception',
    'timeout',
    'failed',
    'refused',
    'oom',
    'killed',
    '5xx',
    'warn',
    'critical',
)


EMPTY_STRUCTURED_REPORT = {
    'summary': '',
    'impact_scope': [],
    'evidence': [],
    'possible_causes': [],
    'recommended_actions': [],
    'risks': [],
    'next_checks': [],
}


def normalize_value(value: Any) -> str:
    return str(value or '').strip().lower()


def match_services_for_alert(alert: AlertEvent, project_id: int | None = None, limit: int = 5) -> dict[str, Any]:
    labels = alert.labels or {}
    warnings: list[str] = []
    queryset = ObservedService.objects.filter(is_active=True).select_related('project')
    if project_id:
        queryset = queryset.filter(project_id=project_id)
    elif alert.labels.get('project_id'):
        queryset = queryset.filter(project_id=alert.labels.get('project_id'))

    if not labels:
        warnings.append('告警标签为空，无法自动匹配服务。')
        return {'best_match': None, 'candidates': [], 'threshold': SERVICE_MATCH_THRESHOLD, 'warnings': warnings}

    candidates = []
    for service in queryset:
        candidate = score_service_for_alert(service, labels)
        if candidate['score'] > 0:
            candidates.append(candidate)

    candidates.sort(key=lambda item: item['score'], reverse=True)
    candidates = candidates[:limit]
    best_match = candidates[0] if candidates and candidates[0]['score'] >= SERVICE_MATCH_THRESHOLD else None
    if candidates and not best_match:
        warnings.append(f"最高匹配分 {candidates[0]['score']} 低于可信阈值 {SERVICE_MATCH_THRESHOLD}，请手动确认服务。")
    elif not candidates:
        warnings.append('没有找到与告警标签匹配的服务映射。')

    return {
        'best_match': best_match,
        'candidates': candidates,
        'threshold': SERVICE_MATCH_THRESHOLD,
        'warnings': warnings,
    }


def score_service_for_alert(service: ObservedService, labels: dict[str, Any]) -> dict[str, Any]:
    normalized_labels = {str(key): normalize_value(value) for key, value in labels.items()}
    service_values = {normalize_value(service.code), normalize_value(service.name)}
    reasons: list[str] = []
    score = 0

    for key in SERVICE_LABEL_KEYS:
        label_value = normalized_labels.get(key)
        if label_value and label_value in service_values:
            score += 70
            reasons.append(f'{key}={label_value} 命中服务标识')
            break

    selector_matches = []
    for selector_name, selector in (
        ('metric_label_selector', service.metric_label_selector or {}),
        ('log_label_selector', service.log_label_selector or {}),
    ):
        for key, value in selector.items():
            if key in normalized_labels and normalized_labels[key] == normalize_value(value):
                selector_matches.append(f'{selector_name}.{key}={value}')
    if selector_matches:
        score += min(50, len(selector_matches) * 20)
        reasons.extend(selector_matches)

    namespace = normalize_value(service.namespace)
    if namespace:
        for key in NAMESPACE_LABEL_KEYS:
            if normalized_labels.get(key) == namespace:
                score += 30
                reasons.append(f'{key}={namespace} 命中命名空间')
                break

    return {
        'id': service.id,
        'name': service.name,
        'code': service.code,
        'project': service.project_id,
        'project_name': getattr(service.project, 'name', None),
        'score': score,
        'reasons': reasons,
    }


def extract_log_highlights(logs: dict[str, Any] | None, limit: int = 30) -> list[dict[str, Any]]:
    if not isinstance(logs, dict):
        return []
    items = logs.get('items') or []
    highlights = []
    for item in items:
        if not isinstance(item, dict):
            continue
        message = str(item.get('message') or '')
        level = str(item.get('level') or '')
        score, matched_keywords = score_log_item(message, level)
        if score <= 0:
            continue
        highlights.append({
            'timestamp': item.get('timestamp'),
            'level': item.get('level'),
            'message': message,
            'service': item.get('service'),
            'instance': item.get('instance'),
            'score': score,
            'matched_keywords': matched_keywords,
        })
    highlights.sort(key=lambda item: item['score'], reverse=True)
    return highlights[:limit]


def score_log_item(message: str, level: str = '') -> tuple[int, list[str]]:
    haystack = f'{level} {message}'.lower()
    matched = []
    score = 0
    for keyword in LOG_HIGHLIGHT_KEYWORDS:
        found = keyword in haystack
        if keyword == '5xx':
            found = found or bool(re.search(r'\b5\d\d\b', haystack))
        if not found:
            continue
        matched.append(keyword)
        if keyword in {'critical', 'error', 'exception', 'timeout', 'failed', 'refused', 'oom', 'killed', '5xx'}:
            score += 30
        else:
            score += 15
    level_normalized = level.lower()
    if level_normalized in {'critical', 'error', 'fatal'}:
        score += 40
    elif level_normalized in {'warn', 'warning'}:
        score += 20
    return score, matched


def build_evidence_index(context: dict[str, Any]) -> list[dict[str, Any]]:
    evidence = []
    for index, item in enumerate(context.get('log_highlights') or [], start=1):
        evidence.append({
            'ref': f'LOG-{index}',
            'type': 'log',
            'title': item.get('message') or 'Log highlight',
            'summary': item.get('message'),
            'timestamp': item.get('timestamp'),
            'source': item.get('service') or item.get('instance'),
            'raw': item,
        })

    for index, item in enumerate(context.get('metrics') or [], start=1):
        evidence.append({
            'ref': f'METRIC-{index}',
            'type': 'metric',
            'title': item.get('name') or item.get('query') or 'Metric query',
            'summary': item.get('query'),
            'source': item.get('name'),
            'raw': item,
        })

    ansflow_events = context.get('ansflow_events') or {}
    for index, item in enumerate(ansflow_events.get('alerts') or [], start=1):
        evidence.append({
            'ref': f'ALERT-{index}',
            'type': 'alert',
            'title': item.get('alert_name') or 'Alert event',
            'summary': f"{item.get('severity')} / {item.get('status')} / {item.get('healing_status')}",
            'timestamp': item.get('create_time'),
            'source': item.get('source'),
            'raw': item,
        })

    for index, item in enumerate(ansflow_events.get('pipeline_runs') or [], start=1):
        evidence.append({
            'ref': f'PIPELINE-{index}',
            'type': 'pipeline_run',
            'title': f"PipelineRun #{item.get('id')}",
            'summary': f"status={item.get('status')}, trigger={item.get('trigger_type')}",
            'timestamp': item.get('create_time'),
            'source': item.get('pipeline_id'),
            'raw': item,
        })

    for index, item in enumerate(ansflow_events.get('ansible_executions') or [], start=1):
        evidence.append({
            'ref': f'ANSIBLE-{index}',
            'type': 'ansible_execution',
            'title': f"AnsibleExecution #{item.get('id')}",
            'summary': f"status={item.get('status')}",
            'timestamp': item.get('create_time'),
            'source': item.get('task_id'),
            'raw': item,
        })

    for index, item in enumerate(ansflow_events.get('approval_tickets') or [], start=1):
        evidence.append({
            'ref': f'APPROVAL-{index}',
            'type': 'approval_ticket',
            'title': item.get('title') or f"ApprovalTicket #{item.get('id')}",
            'summary': f"status={item.get('status')}, resource={item.get('resource_type')}",
            'timestamp': item.get('create_time'),
            'source': item.get('resource_type'),
            'raw': item,
        })

    return evidence


def empty_structured_report() -> dict[str, Any]:
    return json.loads(json.dumps(EMPTY_STRUCTURED_REPORT))


def extract_structured_report(ai_result: str) -> tuple[dict[str, Any], str, str | None]:
    marker = '__STRUCTURED_REPORT__:'
    if marker not in ai_result:
        return empty_structured_report(), ai_result.strip(), 'AI 诊断结果未包含结构化报告块，已降级为 Markdown 展示。'

    prefix, remainder = ai_result.split(marker, 1)
    try:
        report_obj, end_index = _parse_first_json_object(remainder)
        markdown = (prefix + remainder[end_index:]).strip()
        return normalize_structured_report(report_obj), markdown, None
    except Exception as exc:
        markdown = (prefix + remainder).replace(marker, '').strip()
        return empty_structured_report(), markdown, f'结构化诊断报告解析失败：{exc}'


def _parse_first_json_object(text: str) -> tuple[dict[str, Any], int]:
    start = text.find('{')
    if start == -1:
        raise ValueError('未找到 JSON 起始符')
    decoder = json.JSONDecoder()
    obj, end = decoder.raw_decode(text[start:])
    if not isinstance(obj, dict):
        raise ValueError('结构化报告必须是 JSON 对象')
    return obj, start + end


def normalize_structured_report(report: dict[str, Any]) -> dict[str, Any]:
    normalized = empty_structured_report()
    normalized['summary'] = str(report.get('summary') or '')
    normalized['impact_scope'] = _string_list(report.get('impact_scope'))
    normalized['evidence'] = [
        {
            'ref': str(item.get('ref') or ''),
            'finding': str(item.get('finding') or ''),
        }
        for item in _dict_list(report.get('evidence'))
    ]
    normalized['possible_causes'] = [
        {
            'title': str(item.get('title') or ''),
            'confidence': _choice(item.get('confidence'), {'low', 'medium', 'high'}, 'medium'),
            'evidence_refs': _string_list(item.get('evidence_refs')),
        }
        for item in _dict_list(report.get('possible_causes'))
    ]
    normalized['recommended_actions'] = [
        {
            'action': str(item.get('action') or ''),
            'priority': _choice(item.get('priority'), {'low', 'medium', 'high'}, 'medium'),
            'evidence_refs': _string_list(item.get('evidence_refs')),
        }
        for item in _dict_list(report.get('recommended_actions'))
    ]
    normalized['risks'] = _string_list(report.get('risks'))
    normalized['next_checks'] = _string_list(report.get('next_checks'))
    return normalized


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if item not in (None, '')]
    if value in (None, ''):
        return []
    return [str(value)]


def _dict_list(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _choice(value: Any, allowed: set[str], default: str) -> str:
    normalized = str(value or '').lower()
    return normalized if normalized in allowed else default
