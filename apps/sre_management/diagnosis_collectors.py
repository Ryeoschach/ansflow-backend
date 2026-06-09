from __future__ import annotations

from typing import Any

from django.db.models import Q

from .models import AlertEvent
from .diagnosis_security import redact_sensitive_data, redact_sensitive_text


def template_collection_config(template_snapshot: dict[str, Any] | None) -> dict[str, Any]:
    content = (template_snapshot or {}).get('content') or {}
    return content.get('context_collection') or {}


def _template_log_keywords(template_snapshot: dict[str, Any] | None) -> list[str]:
    content = (template_snapshot or {}).get('content') or {}
    keywords = content.get('log_keywords') or []
    return [str(item).lower() for item in keywords if str(item).strip()]


def _highlight_text_lines(text: Any, keywords: list[str], limit: int = 30) -> list[dict[str, Any]]:
    if not text:
        return []
    keywords = keywords or ['error', 'failed', 'exception', 'timeout']
    highlights = []
    for line_no, line in enumerate(str(text).splitlines(), start=1):
        lower = line.lower()
        matched = [keyword for keyword in keywords if keyword in lower]
        if matched:
            highlights.append({
                'line_no': line_no,
                'line': line[:1000],
                'matched_keywords': matched,
            })
        if len(highlights) >= limit:
            break
    return highlights


def _project_alert_filter(project) -> Q:
    if not project:
        return Q(pk__in=[])
    return (
        Q(labels__project_id=project.id)
        | Q(labels__project_id=str(project.id))
        | Q(labels__project=project.code)
        | Q(labels__project_code=project.code)
    )


def _project_approval_filter(project) -> Q:
    if not project:
        return Q(pk__in=[])
    return (
        Q(payload__project=project.id)
        | Q(payload__project=str(project.id))
        | Q(payload__project_id=project.id)
        | Q(payload__project_id=str(project.id))
        | Q(payload__project_code=project.code)
    )


class AnsFlowEventCollector:
    def collect(self, run, start, end) -> dict[str, list[dict[str, Any]]]:
        from apps.approval_center.models import ApprovalTicket
        from apps.pipeline_management.models import PipelineRun
        from apps.task_management.models import AnsibleExecution

        pipeline_filter = {'pipeline__project_id': run.project_id} if run.project_id else {}
        ansible_filter = {'task__project_id': run.project_id} if run.project_id else {}
        alerts = AlertEvent.objects.filter(create_time__range=(start, end))
        approvals = ApprovalTicket.objects.filter(create_time__range=(start, end))
        if run.project_id:
            alerts = alerts.filter(_project_alert_filter(run.project))
            approvals = approvals.filter(_project_approval_filter(run.project))
        return {
            'alerts': list(alerts.values(
                'id', 'alert_name', 'severity', 'status', 'source', 'labels', 'annotations',
                'healing_status', 'create_time',
            )[:20]),
            'pipeline_runs': list(PipelineRun.objects.filter(
                create_time__range=(start, end),
                **pipeline_filter,
            ).values(
                'id', 'pipeline_id', 'status', 'trigger_type', 'create_time', 'update_time',
            )[:20]),
            'ansible_executions': list(AnsibleExecution.objects.filter(
                create_time__range=(start, end),
                **ansible_filter,
            ).values(
                'id', 'task_id', 'status', 'create_time', 'update_time',
            )[:20]),
            'approval_tickets': list(approvals.values(
                'id', 'title', 'status', 'resource_type', 'create_time', 'audit_time',
            )[:20]),
        }

    def collect_into(self, context: dict[str, Any], run, start, end) -> None:
        events = self.collect(run, start, end)
        context['ansflow_events'] = events
        context['collection_summary']['ansflow_events'] = {
            'status': 'success',
            'count': sum(len(value) for value in events.values()),
        }


class CiCdContextCollector:
    def collect(
        self,
        run,
        start,
        end,
        template_snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        from apps.approval_center.models import ApprovalTicket
        from apps.pipeline_management.models import PipelineNodeRun, PipelineRun
        from apps.task_management.models import AnsibleExecution, TaskLog
        from django.db.models import Q

        query_params = run.query_params or {}
        collection = template_collection_config(template_snapshot)
        keywords = _template_log_keywords(template_snapshot)
        context = {
            'target': {
                'pipeline_run_id': query_params.get('pipeline_run_id'),
                'pipeline_node_run_id': query_params.get('pipeline_node_run_id'),
                'ansible_execution_id': query_params.get('ansible_execution_id'),
            },
            'pipeline_run': None,
            'failed_nodes': [],
            'node_log_highlights': [],
            'ansible_execution': None,
            'ansible_task_logs': [],
            'ansible_task_log_highlights': [],
            'approval_records': [],
            'related_alerts': [],
            'collection_summary': {
                'pipeline_run': {'status': 'skipped', 'count': 0},
                'failed_nodes': {'status': 'skipped', 'count': 0},
                'node_logs': {'status': 'skipped', 'count': 0},
                'ansible_execution': {'status': 'skipped', 'count': 0},
                'ansible_task_logs': {'status': 'skipped', 'count': 0},
                'approval_records': {'status': 'skipped', 'count': 0},
                'related_alerts': {'status': 'skipped', 'count': 0},
            },
        }

        pipeline_run = None
        pipeline_run_id = query_params.get('pipeline_run_id')
        node_run_id = query_params.get('pipeline_node_run_id')
        ansible_execution_id = query_params.get('ansible_execution_id')
        if pipeline_run_id:
            pipeline_run = PipelineRun.objects.select_related('pipeline').filter(
                id=pipeline_run_id,
                pipeline__project_id=run.project_id,
            ).first()
        elif node_run_id:
            node_run = PipelineNodeRun.objects.select_related('run', 'run__pipeline').filter(
                id=node_run_id,
                run__pipeline__project_id=run.project_id,
            ).first()
            pipeline_run = node_run.run if node_run else None

        if pipeline_run and collection.get('pipeline_run', True):
            context['pipeline_run'] = {
                'id': pipeline_run.id,
                'pipeline_id': pipeline_run.pipeline_id,
                'pipeline_name': getattr(pipeline_run.pipeline, 'name', None),
                'status': pipeline_run.status,
                'trigger_type': pipeline_run.trigger_type,
                'start_time': pipeline_run.start_time,
                'end_time': pipeline_run.end_time,
                'create_time': pipeline_run.create_time,
                'extra_vars': redact_sensitive_data(pipeline_run.extra_vars),
            }
            context['collection_summary']['pipeline_run'] = {'status': 'success', 'count': 1}

        if collection.get('failed_nodes', True):
            node_queryset = PipelineNodeRun.objects.all()
            if node_run_id:
                node_queryset = node_queryset.filter(id=node_run_id)
            elif pipeline_run:
                node_queryset = node_queryset.filter(run=pipeline_run, status='failed')
            else:
                node_queryset = node_queryset.filter(
                    create_time__range=(start, end),
                    status='failed',
                    run__pipeline__project_id=run.project_id,
                )
            failed_nodes = list(node_queryset.values(
                'id', 'run_id', 'node_id', 'node_type', 'node_label', 'status',
                'approval_time', 'approval_comment', 'start_time', 'end_time', 'create_time',
                'output_data',
            )[:20])
            context['failed_nodes'] = redact_sensitive_data(failed_nodes)
            context['collection_summary']['failed_nodes'] = {
                'status': 'success',
                'count': len(failed_nodes),
            }

            if collection.get('node_logs', True):
                log_nodes = PipelineNodeRun.objects.filter(id__in=[item['id'] for item in failed_nodes])
                for node in log_nodes:
                    for item in _highlight_text_lines(
                        redact_sensitive_text(node.logs),
                        keywords,
                        limit=10,
                    ):
                        item.update({
                            'node_run_id': node.id,
                            'node_id': node.node_id,
                            'node_label': node.node_label,
                        })
                        context['node_log_highlights'].append(item)
                context['collection_summary']['node_logs'] = {
                    'status': 'success',
                    'count': len(context['node_log_highlights']),
                }

        if collection.get('ansible_execution') and ansible_execution_id:
            execution = AnsibleExecution.objects.select_related('task').filter(
                id=ansible_execution_id,
                task__project_id=run.project_id,
            ).first()
            if execution:
                context['ansible_execution'] = {
                    'id': execution.id,
                    'task_id': execution.task_id,
                    'task_name': getattr(execution.task, 'name', None),
                    'status': execution.status,
                    'result_summary': redact_sensitive_data(execution.result_summary),
                    'extra_vars_snapshot': redact_sensitive_data(execution.extra_vars_snapshot),
                    'start_time': execution.start_time,
                    'end_time': execution.end_time,
                    'create_time': execution.create_time,
                }
                context['collection_summary']['ansible_execution'] = {'status': 'success', 'count': 1}
                if collection.get('ansible_task_logs', True):
                    logs = list(TaskLog.objects.filter(execution=execution).values(
                        'id', 'host', 'output', 'create_time',
                    )[:50])
                    context['ansible_task_logs'] = redact_sensitive_data(logs)
                    for log in logs:
                        for item in _highlight_text_lines(
                            redact_sensitive_text(log.get('output')),
                            keywords,
                            limit=5,
                        ):
                            item.update({
                                'id': log.get('id'),
                                'host': log.get('host'),
                                'create_time': log.get('create_time'),
                            })
                            context['ansible_task_log_highlights'].append(item)
                    context['collection_summary']['ansible_task_logs'] = {
                        'status': 'success',
                        'count': len(logs),
                    }

        if collection.get('approval_records', True):
            approvals = ApprovalTicket.objects.filter(
                create_time__range=(start, end),
            ).filter(_project_approval_filter(run.project))
            if pipeline_run:
                approvals = approvals.filter(
                    Q(target_id=str(pipeline_run.id)) | Q(title__icontains=str(pipeline_run.id)),
                )
            context['approval_records'] = list(approvals.values(
                'id', 'title', 'status', 'resource_type', 'target_id', 'create_time', 'audit_time',
            )[:20])
            context['collection_summary']['approval_records'] = {
                'status': 'success',
                'count': len(context['approval_records']),
            }

        if collection.get('related_alerts', True):
            alerts = AlertEvent.objects.filter(
                create_time__range=(start, end),
            ).filter(_project_alert_filter(run.project))
            if run.alert_id:
                alerts = AlertEvent.objects.filter(
                    Q(id=run.alert_id) | Q(id__in=alerts.values('id')),
                )
            context['related_alerts'] = redact_sensitive_data(list(alerts.values(
                'id', 'alert_name', 'severity', 'status', 'source',
                'labels', 'annotations', 'healing_status', 'create_time',
            )[:20]))
            context['collection_summary']['related_alerts'] = {
                'status': 'success',
                'count': len(context['related_alerts']),
            }

        return context

    def collect_into(
        self,
        context: dict[str, Any],
        run,
        start,
        end,
        template_snapshot: dict[str, Any],
    ) -> None:
        ci_cd_context = self.collect(run, start, end, template_snapshot)
        context['ci_cd_context'] = ci_cd_context
        context['collection_summary']['ci_cd_context'] = {
            'status': 'success',
            'count': sum(
                summary.get('count', 0)
                for summary in (ci_cd_context.get('collection_summary') or {}).values()
                if isinstance(summary, dict)
            ),
        }


class DiagnosisEvidenceBuilder:
    def build(self, context: dict[str, Any]) -> list[dict[str, Any]]:
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

        for log_context in context.get('log_contexts') or []:
            datasource = log_context.get('datasource') or {}
            datasource_id = datasource.get('id') or 'unknown'
            datasource_name = datasource.get('name') or datasource_id
            for index, item in enumerate(log_context.get('highlights') or [], start=1):
                ref = item.get('evidence_id') or f'log:{datasource_id}:{index}'
                evidence.append({
                    'ref': ref,
                    'type': 'log',
                    'title': item.get('message') or f'Log highlight from {datasource_name}',
                    'summary': item.get('message'),
                    'timestamp': item.get('timestamp'),
                    'source': datasource_name,
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

        for metric_context in context.get('metric_contexts') or []:
            datasource = metric_context.get('datasource') or {}
            datasource_id = datasource.get('id') or 'unknown'
            datasource_name = datasource.get('name') or datasource_id
            for index, item in enumerate(metric_context.get('metrics') or [], start=1):
                ref = item.get('evidence_id') or f'metric:{datasource_id}:{index}'
                evidence.append({
                    'ref': ref,
                    'type': 'metric',
                    'title': item.get('name') or item.get('query') or f'Metric from {datasource_name}',
                    'summary': item.get('query'),
                    'source': datasource_name,
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

        ci_cd_context = context.get('ci_cd_context') or {}
        for index, item in enumerate(ci_cd_context.get('failed_nodes') or [], start=1):
            evidence.append({
                'ref': f'NODE-{index}',
                'type': 'pipeline_node',
                'title': item.get('node_label') or item.get('node_id') or f"NodeRun #{item.get('id')}",
                'summary': f"status={item.get('status')}, type={item.get('node_type')}",
                'timestamp': item.get('end_time') or item.get('create_time'),
                'source': item.get('run_id'),
                'raw': item,
            })

        for index, item in enumerate(ci_cd_context.get('node_log_highlights') or [], start=1):
            evidence.append({
                'ref': f'NODELOG-{index}',
                'type': 'pipeline_node_log',
                'title': item.get('node_label') or item.get('node_id') or 'Node log highlight',
                'summary': item.get('line'),
                'timestamp': item.get('timestamp'),
                'source': item.get('node_id'),
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

        for index, item in enumerate(ci_cd_context.get('ansible_task_log_highlights') or [], start=1):
            evidence.append({
                'ref': f'TASKLOG-{index}',
                'type': 'ansible_task_log',
                'title': item.get('host') or f"TaskLog #{item.get('id')}",
                'summary': item.get('line') or item.get('output'),
                'timestamp': item.get('create_time'),
                'source': item.get('host'),
                'raw': item,
            })

        for index, item in enumerate(ci_cd_context.get('related_alerts') or [], start=1):
            evidence.append({
                'ref': f'RELATED-ALERT-{index}',
                'type': 'alert',
                'title': item.get('alert_name') or 'Related alert',
                'summary': f"{item.get('severity')} / {item.get('status')}",
                'timestamp': item.get('create_time'),
                'source': item.get('source'),
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

        runtime = context.get('runtime_context') or {}
        for index, item in enumerate(runtime.get('hosts') or [], start=1):
            evidence.append({
                'ref': f'HOST-{index}',
                'type': 'host',
                'title': item.get('hostname') or f"Host #{item.get('id')}",
                'summary': f"status={item.get('status')}, os={item.get('os_type')}",
                'source': item.get('private_ip') or item.get('ip_address'),
                'raw': item,
            })
        for index, item in enumerate(runtime.get('pods') or [], start=1):
            evidence.append({
                'ref': f'K8S-POD-{index}',
                'type': 'k8s_pod',
                'title': item.get('name') or 'Pod',
                'summary': f"status={item.get('status')}, restarts={item.get('restarts')}",
                'timestamp': item.get('creation_timestamp'),
                'source': item.get('namespace'),
                'raw': item,
            })
        for index, item in enumerate(runtime.get('k8s_events') or [], start=1):
            evidence.append({
                'ref': f'K8S-EVENT-{index}',
                'type': 'k8s_event',
                'title': item.get('reason') or item.get('object') or 'Kubernetes event',
                'summary': item.get('message'),
                'timestamp': item.get('last_timestamp') or item.get('first_timestamp'),
                'source': item.get('namespace'),
                'raw': item,
            })

        return evidence
