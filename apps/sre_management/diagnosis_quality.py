from __future__ import annotations

from datetime import datetime
from typing import Any

from django.db.models import Avg, Count, Q
from django.db.models.functions import TruncDate
from django.utils import timezone

from .diagnosis_security import redact_sensitive_data
from .models import (
    DiagnosisFeedback,
    DiagnosisReplayCase,
    DiagnosisRun,
    DiagnosisTemplate,
    DiagnosisTemplateVersion,
)


CONFIDENCE_SCORES = {'low': 0.35, 'medium': 0.65, 'high': 0.9}


def create_template_version(
    template: DiagnosisTemplate,
    user=None,
    change_summary: str | None = None,
) -> DiagnosisTemplateVersion:
    return DiagnosisTemplateVersion.objects.create(
        template=template,
        version=template.version,
        name=template.name,
        description=template.description,
        category=template.category,
        content=redact_sensitive_data(template.content),
        change_summary=change_summary,
        created_by=user,
    )


def build_diagnosis_timeline(context: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []

    def append(timestamp, kind, title, summary=None, ref=None, severity='info'):
        if not timestamp:
            return
        items.append({
            'timestamp': timestamp.isoformat() if hasattr(timestamp, 'isoformat') else str(timestamp),
            'kind': kind,
            'title': title,
            'summary': summary,
            'ref': ref,
            'severity': severity,
        })

    ci_cd = context.get('ci_cd_context') or {}
    pipeline = ci_cd.get('pipeline_run') or {}
    append(
        pipeline.get('start_time') or pipeline.get('create_time'),
        'deployment',
        pipeline.get('pipeline_name') or f"PipelineRun #{pipeline.get('id')}",
        f"status={pipeline.get('status')}, trigger={pipeline.get('trigger_type')}",
        'PIPELINE-1',
        'warning' if pipeline.get('status') == 'failed' else 'info',
    )
    for index, node in enumerate(ci_cd.get('failed_nodes') or [], start=1):
        append(
            node.get('end_time') or node.get('create_time'),
            'pipeline_node',
            node.get('node_label') or node.get('node_id') or f"Node #{node.get('id')}",
            f"type={node.get('node_type')}, status={node.get('status')}",
            f'NODE-{index}',
            'error',
        )
    for index, alert in enumerate((context.get('ansflow_events') or {}).get('alerts') or [], start=1):
        append(
            alert.get('create_time'),
            'alert',
            alert.get('alert_name') or 'Alert',
            f"{alert.get('severity')} / {alert.get('status')}",
            f'ALERT-{index}',
            'error' if str(alert.get('severity')).lower() in {'critical', 'error'} else 'warning',
        )
    runtime = context.get('runtime_context') or {}
    for index, event in enumerate(runtime.get('k8s_events') or [], start=1):
        append(
            event.get('last_timestamp') or event.get('first_timestamp'),
            'k8s_event',
            event.get('reason') or event.get('object') or 'Kubernetes event',
            event.get('message'),
            f'K8S-EVENT-{index}',
            'error' if event.get('type') == 'Warning' else 'info',
        )
    for log_context in context.get('log_contexts') or []:
        for item in log_context.get('highlights') or []:
            append(
                item.get('timestamp'),
                'log',
                item.get('message') or 'Log highlight',
                ', '.join(item.get('matched_keywords') or []),
                item.get('evidence_id'),
                'error',
            )
    items.sort(key=lambda item: item['timestamp'])
    return items[:200]


def build_correlation_analysis(context: dict[str, Any]) -> dict[str, Any]:
    evidence = context.get('evidence_index') or []
    refs_by_type: dict[str, list[str]] = {}
    for item in evidence:
        refs_by_type.setdefault(str(item.get('type')), []).append(str(item.get('ref')))

    candidates = []
    ci_cd = context.get('ci_cd_context') or {}
    runtime = context.get('runtime_context') or {}
    metric_anomalies = [
        metric
        for metric_context in context.get('metric_contexts') or []
        for metric in metric_context.get('metrics') or []
        if abs(float((metric.get('summary') or {}).get('change_percent') or 0)) >= 30
    ]
    log_count = sum(cluster.get('count', 0) for cluster in context.get('log_clusters') or [])

    if ci_cd.get('pipeline_run') and (log_count or metric_anomalies):
        refs = (refs_by_type.get('pipeline_run') or []) + (refs_by_type.get('log') or [])[:3]
        refs += (refs_by_type.get('metric') or [])[:3]
        candidates.append({
            'title': '发布变更与服务异常时间相关',
            'confidence_score': min(0.92, 0.58 + 0.04 * len(refs)),
            'evidence_refs': refs,
            'basis': '发布事件附近同时出现日志错误或指标显著变化。',
            'is_inference': True,
        })
    unhealthy_pods = [
        item for item in runtime.get('pods') or []
        if item.get('status') not in {'Running', 'Succeeded'} or int(item.get('restarts') or 0) > 0
    ]
    if unhealthy_pods:
        refs = (refs_by_type.get('k8s_pod') or [])[:5] + (refs_by_type.get('k8s_event') or [])[:5]
        candidates.append({
            'title': 'Kubernetes 工作负载健康异常',
            'confidence_score': min(0.95, 0.68 + 0.03 * len(unhealthy_pods)),
            'evidence_refs': refs,
            'basis': f'发现 {len(unhealthy_pods)} 个非健康或发生重启的 Pod。',
            'is_inference': False,
        })
    unhealthy_hosts = [item for item in runtime.get('hosts') or [] if item.get('status') not in {1, '1'}]
    if unhealthy_hosts:
        candidates.append({
            'title': '主机资产状态异常',
            'confidence_score': min(0.9, 0.65 + 0.05 * len(unhealthy_hosts)),
            'evidence_refs': (refs_by_type.get('host') or [])[:5],
            'basis': f'发现 {len(unhealthy_hosts)} 台主机处于非在线状态。',
            'is_inference': False,
        })
    if metric_anomalies:
        candidates.append({
            'title': '关键指标发生显著突变',
            'confidence_score': min(0.9, 0.55 + 0.06 * len(metric_anomalies)),
            'evidence_refs': (refs_by_type.get('metric') or [])[:8],
            'basis': f'发现 {len(metric_anomalies)} 个变化率超过 30% 的指标。',
            'is_inference': True,
        })
    candidates.sort(key=lambda item: item['confidence_score'], reverse=True)
    return {
        'root_cause_candidates': candidates,
        'signal_counts': {
            'evidence': len(evidence),
            'log_clusters': len(context.get('log_clusters') or []),
            'metric_anomalies': len(metric_anomalies),
            'unhealthy_pods': len(unhealthy_pods),
            'unhealthy_hosts': len(unhealthy_hosts),
        },
    }


def score_structured_report(report: dict[str, Any], evidence: list[dict[str, Any]]) -> dict[str, float]:
    valid_refs = {str(item.get('ref')) for item in evidence if item.get('ref')}
    referenced = set()
    confidence_values = []
    for item in report.get('evidence') or []:
        if item.get('ref'):
            referenced.add(str(item['ref']))
    for item in report.get('possible_causes') or []:
        referenced.update(str(ref) for ref in item.get('evidence_refs') or [])
        confidence_values.append(CONFIDENCE_SCORES.get(item.get('confidence'), 0.5))
    for item in report.get('recommended_actions') or []:
        referenced.update(str(ref) for ref in item.get('evidence_refs') or [])
    valid_referenced = referenced & valid_refs
    evidence_coverage = len(valid_referenced) / max(1, len(referenced))
    confidence = sum(confidence_values) / max(1, len(confidence_values))
    completeness_checks = [
        bool(report.get('summary')),
        bool(report.get('evidence')),
        bool(report.get('possible_causes')),
        bool(report.get('recommended_actions')),
        bool(report.get('next_checks')),
    ]
    completeness = sum(completeness_checks) / len(completeness_checks)
    quality = round((evidence_coverage * 0.45 + confidence * 0.25 + completeness * 0.30) * 100, 2)
    return {
        'evidence_coverage': round(evidence_coverage, 4),
        'confidence_score': round(confidence, 4),
        'quality_score': quality,
    }


def evaluate_replay(case: DiagnosisReplayCase, report: dict[str, Any]) -> dict[str, Any]:
    expected = case.expected or {}
    text = ' '.join([
        str(report.get('summary') or ''),
        *[str(item.get('title') or '') for item in report.get('possible_causes') or []],
    ]).lower()
    expected_keywords = [str(item).lower() for item in expected.get('root_cause_keywords') or []]
    matched_keywords = [item for item in expected_keywords if item in text]
    expected_refs = {str(item) for item in expected.get('evidence_refs') or []}
    actual_refs = {
        str(ref)
        for item in report.get('possible_causes') or []
        for ref in item.get('evidence_refs') or []
    }
    keyword_score = len(matched_keywords) / max(1, len(expected_keywords))
    evidence_score = len(expected_refs & actual_refs) / max(1, len(expected_refs))
    score = round((keyword_score * 0.6 + evidence_score * 0.4) * 100, 2)
    threshold = float(expected.get('minimum_score') or 60)
    return {
        'score': score,
        'passed': score >= threshold,
        'threshold': threshold,
        'matched_keywords': matched_keywords,
        'missing_keywords': sorted(set(expected_keywords) - set(matched_keywords)),
        'matched_evidence_refs': sorted(expected_refs & actual_refs),
        'missing_evidence_refs': sorted(expected_refs - actual_refs),
    }


def diagnosis_quality_summary(project_id=None) -> dict[str, Any]:
    runs = DiagnosisRun.objects.all()
    feedbacks = DiagnosisFeedback.objects.all()
    replay_cases = DiagnosisReplayCase.objects.all()
    if project_id:
        runs = runs.filter(project_id=project_id)
        feedbacks = feedbacks.filter(run__project_id=project_id)
        replay_cases = replay_cases.filter(project_id=project_id)
    run_stats = runs.aggregate(
        total=Count('id'),
        success=Count('id', filter=Q(status='success')),
        failed=Count('id', filter=Q(status='failed')),
        avg_quality=Avg('quality_score'),
        avg_confidence=Avg('confidence_score'),
        avg_evidence_coverage=Avg('evidence_coverage'),
    )
    feedback_stats = feedbacks.aggregate(
        total=Count('id'),
        avg_accuracy=Avg('accuracy_rating'),
        avg_evidence=Avg('evidence_rating'),
        avg_actionability=Avg('actionability_rating'),
        adopted=Count('id', filter=Q(recommendation_adopted=True)),
        root_cause_correct=Count('id', filter=Q(root_cause_correct=True)),
    )
    latest_results = [
        case.results.order_by('-create_time').first()
        for case in replay_cases.prefetch_related('results')
    ]
    latest_results = [result for result in latest_results if result]
    passed = sum(1 for result in latest_results if result.passed)
    total = run_stats['total'] or 0
    trend = list(
        runs.filter(create_time__gte=timezone.now() - timezone.timedelta(days=30))
        .annotate(day=TruncDate('create_time'))
        .values('day')
        .annotate(
            total=Count('id'),
            success=Count('id', filter=Q(status='success')),
            avg_quality=Avg('quality_score'),
        )
        .order_by('day')
    )
    by_template = list(
        runs.values('template__code', 'template__name')
        .annotate(
            total=Count('id'),
            success=Count('id', filter=Q(status='success')),
            avg_quality=Avg('quality_score'),
            avg_confidence=Avg('confidence_score'),
        )
        .order_by('-total')[:20]
    )
    return {
        'generated_at': timezone.now().isoformat(),
        'runs': {
            **run_stats,
            'success_rate': round((run_stats['success'] or 0) * 100 / max(1, total), 2),
        },
        'feedback': {
            **feedback_stats,
            'adoption_rate': round((feedback_stats['adopted'] or 0) * 100 / max(1, feedback_stats['total'] or 0), 2),
            'root_cause_accuracy': round((feedback_stats['root_cause_correct'] or 0) * 100 / max(1, feedback_stats['total'] or 0), 2),
        },
        'replay': {
            'cases': replay_cases.count(),
            'evaluated': len(latest_results),
            'passed': passed,
            'pass_rate': round(passed * 100 / max(1, len(latest_results)), 2),
        },
        'trend': [{
            **item,
            'day': item['day'].isoformat() if item['day'] else None,
            'success_rate': round((item['success'] or 0) * 100 / max(1, item['total'] or 0), 2),
        } for item in trend],
        'templates': [{
            'code': item['template__code'] or 'untemplated',
            'name': item['template__name'] or 'Untemplated',
            'total': item['total'],
            'success': item['success'],
            'avg_quality': item['avg_quality'],
            'avg_confidence': item['avg_confidence'],
            'success_rate': round((item['success'] or 0) * 100 / max(1, item['total'] or 0), 2),
        } for item in by_template],
    }
