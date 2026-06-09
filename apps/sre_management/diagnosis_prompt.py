from __future__ import annotations

import json
from typing import Any, Callable


class DiagnosisPromptContextBuilder:
    DEFAULT_MAX_CHARS = 24000
    SECTION_BUDGETS = {
        'evidence_index': 4500,
        'logs': 4500,
        'metrics': 3500,
        'ci_cd_context': 4500,
        'ansflow_events': 2500,
    }

    def __init__(self, max_chars: int = DEFAULT_MAX_CHARS):
        self.max_chars = max(1000, max_chars)
        scale = min(1.0, self.max_chars / self.DEFAULT_MAX_CHARS)
        self.section_budgets = {
            key: max(300, int(value * scale))
            for key, value in self.SECTION_BUDGETS.items()
        }

    def build(self, context: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        original_chars = len(self._serialize(context))
        counts: dict[str, dict[str, int]] = {}
        compact_context = {
            'diagnosis': self._compact_value(context.get('diagnosis')),
            'project': self._compact_value(context.get('project')),
            'template': self._compact_value(context.get('template')),
            'service': self._compact_value(context.get('service')),
            'service_match': self._compact_value(context.get('service_match')),
            'source_alert': self._compact_value(context.get('source_alert')),
            'warnings': self._compact_value(context.get('warnings')),
            'collection_summary': self._compact_value(context.get('collection_summary')),
        }

        compact_context['evidence_index'] = self._build_evidence(context, counts)
        compact_context['logs'] = self._build_logs(context, counts)
        compact_context['metrics'] = self._build_metrics(context, counts)
        compact_context['ci_cd_context'] = self._build_ci_cd_context(context, counts)
        compact_context['ansflow_events'] = self._build_ansflow_events(context, counts)

        prompt_context = self._serialize(compact_context)
        prompt_context = self._enforce_final_budget(compact_context, counts, prompt_context)
        final_chars = len(prompt_context)
        removed = {
            key: max(0, value['available'] - value['included'])
            for key, value in counts.items()
            if value['available'] > value['included']
        }
        removed_count = sum(removed.values())
        summary = {
            'status': 'success',
            'compressed': final_chars < original_chars,
            'truncated': removed_count > 0,
            'budget_chars': self.max_chars,
            'original_chars': original_chars,
            'final_chars': final_chars,
            'removed_count': removed_count,
            'removed': removed,
            'included': {key: value['included'] for key, value in counts.items()},
            'available': {key: value['available'] for key, value in counts.items()},
        }
        return prompt_context, summary

    def _build_evidence(
        self,
        context: dict[str, Any],
        counts: dict[str, dict[str, int]],
    ) -> list[dict[str, Any]]:
        items = sorted(
            context.get('evidence_index') or [],
            key=self._evidence_score,
            reverse=True,
        )
        self._record_count(
            counts,
            'evidence_raw_payloads',
            sum(1 for item in items if item.get('raw') is not None),
            0,
        )
        return self._fit_items(
            'evidence',
            items,
            self.section_budgets['evidence_index'],
            counts,
            lambda item: {
                'ref': item.get('ref'),
                'type': item.get('type'),
                'title': self._compact_string(item.get('title'), 300),
                'summary': self._compact_string(item.get('summary'), 700),
                'timestamp': item.get('timestamp'),
                'source': item.get('source'),
            },
        )

    def _build_logs(
        self,
        context: dict[str, Any],
        counts: dict[str, dict[str, int]],
    ) -> dict[str, Any]:
        sources = []
        highlights = []
        legacy_highlights = context.get('log_highlights') or []
        for item in legacy_highlights:
            highlights.append({**item, '_source_name': item.get('service') or item.get('instance')})

        raw_log_items = 0
        for log_context in context.get('log_contexts') or []:
            datasource = log_context.get('datasource') or {}
            sources.append({
                'datasource': self._compact_value(datasource),
                'query': self._compact_string(log_context.get('query'), 700),
                'time_range': self._compact_value(log_context.get('time_range')),
                'count': log_context.get('count'),
                'highlight_count': log_context.get('highlight_count'),
            })
            raw_log_items += len(log_context.get('items') or [])
            for item in log_context.get('highlights') or []:
                highlights.append({
                    **item,
                    '_source_name': datasource.get('name'),
                    '_datasource_id': datasource.get('id'),
                })

        highlights.sort(key=lambda item: int(item.get('score') or 0), reverse=True)
        included = self._fit_items(
            'log_highlights',
            highlights,
            self.section_budgets['logs'],
            counts,
            lambda item: {
                'evidence_id': item.get('evidence_id'),
                'timestamp': item.get('timestamp'),
                'level': item.get('level'),
                'message': self._compact_string(
                    item.get('message'),
                    min(700, max(150, self.section_budgets['logs'] - 250)),
                ),
                'service': item.get('service'),
                'instance': item.get('instance'),
                'source': item.get('_source_name'),
                'datasource_id': item.get('_datasource_id'),
                'score': item.get('score'),
                'matched_keywords': item.get('matched_keywords'),
            },
        )
        self._record_count(counts, 'raw_log_items', raw_log_items, 0)
        return {'sources': sources, 'highlights': included}

    def _build_metrics(
        self,
        context: dict[str, Any],
        counts: dict[str, dict[str, int]],
    ) -> dict[str, Any]:
        sources = []
        metrics = []
        metric_contexts = context.get('metric_contexts') or []
        if metric_contexts:
            for metric_context in metric_contexts:
                datasource = metric_context.get('datasource') or {}
                sources.append({
                    'datasource': self._compact_value(datasource),
                    'time_range': self._compact_value(metric_context.get('time_range')),
                    'count': metric_context.get('count'),
                })
                for item in metric_context.get('metrics') or []:
                    metrics.append({**item, '_datasource': datasource})
        else:
            metrics = list(context.get('metrics') or [])

        included = self._fit_items(
            'metrics',
            metrics,
            self.section_budgets['metrics'],
            counts,
            lambda item: {
                'evidence_id': item.get('evidence_id'),
                'name': item.get('name'),
                'query': self._compact_string(item.get('query'), 700),
                'datasource': self._compact_value(item.get('_datasource') or item.get('datasource')),
                'result': self._compact_value(item.get('result'), max_depth=3, max_items=5, max_string=500),
            },
        )
        return {'sources': sources, 'items': included}

    def _build_ci_cd_context(
        self,
        context: dict[str, Any],
        counts: dict[str, dict[str, int]],
    ) -> dict[str, Any]:
        ci_cd = context.get('ci_cd_context') or {}
        node_logs = sorted(
            ci_cd.get('node_log_highlights') or [],
            key=lambda item: len(item.get('matched_keywords') or []),
            reverse=True,
        )
        task_logs = sorted(
            ci_cd.get('ansible_task_log_highlights') or [],
            key=lambda item: len(item.get('matched_keywords') or []),
            reverse=True,
        )
        budget = self.section_budgets['ci_cd_context']
        node_log_budget = int(budget * 0.35)
        task_log_budget = int(budget * 0.30)
        failed_node_budget = int(budget * 0.20)
        approval_budget = max(250, budget - node_log_budget - task_log_budget - failed_node_budget)
        node_log_limit = min(900, max(100, node_log_budget - 180))
        task_log_limit = min(900, max(100, task_log_budget - 180))
        self._record_count(counts, 'ansible_task_logs', len(ci_cd.get('ansible_task_logs') or []), 0)
        return {
            'target': self._compact_value(ci_cd.get('target')),
            'pipeline_run': self._compact_value(ci_cd.get('pipeline_run'), max_depth=3, max_items=8),
            'failed_nodes': self._fit_items(
                'failed_nodes',
                ci_cd.get('failed_nodes') or [],
                failed_node_budget,
                counts,
                lambda item: {
                    key: self._compact_value(value, max_depth=2, max_items=5, max_string=500)
                    for key, value in item.items()
                },
            ),
            'node_log_highlights': self._fit_items(
                'node_log_highlights',
                node_logs,
                node_log_budget,
                counts,
                lambda item: {
                    **{key: value for key, value in item.items() if key != 'line'},
                    'line': self._compact_string(item.get('line'), node_log_limit),
                },
            ),
            'ansible_execution': self._compact_value(
                ci_cd.get('ansible_execution'),
                max_depth=3,
                max_items=8,
                max_string=700,
            ),
            'ansible_task_log_highlights': self._fit_items(
                'ansible_task_log_highlights',
                task_logs,
                task_log_budget,
                counts,
                lambda item: {
                    **{key: value for key, value in item.items() if key not in {'line', 'output'}},
                    'line': self._compact_string(item.get('line') or item.get('output'), task_log_limit),
                },
            ),
            'approval_records': self._fit_items(
                'approval_records',
                ci_cd.get('approval_records') or [],
                approval_budget,
                counts,
                lambda item: self._compact_value(item, max_depth=2, max_items=8, max_string=500),
            ),
            'collection_summary': self._compact_value(ci_cd.get('collection_summary')),
        }

    def _build_ansflow_events(
        self,
        context: dict[str, Any],
        counts: dict[str, dict[str, int]],
    ) -> dict[str, Any]:
        events = context.get('ansflow_events') or {}
        budget = self.section_budgets['ansflow_events']
        keys = ('alerts', 'pipeline_runs', 'ansible_executions', 'approval_tickets')
        per_group = max(250, budget // len(keys))
        result = {}
        for key in keys:
            items = events.get(key) or []
            if key == 'alerts':
                items = sorted(
                    items,
                    key=lambda item: {'critical': 4, 'error': 3, 'warning': 2}.get(
                        str(item.get('severity') or '').lower(),
                        1,
                    ),
                    reverse=True,
                )
            result[key] = self._fit_items(
                f'ansflow_{key}',
                items,
                per_group,
                counts,
                lambda item: self._compact_value(item, max_depth=3, max_items=10, max_string=500),
            )
        return result

    def _fit_items(
        self,
        key: str,
        items: list[Any],
        budget: int,
        counts: dict[str, dict[str, int]],
        projector: Callable[[Any], Any],
    ) -> list[Any]:
        included = []
        for item in items:
            compact_item = projector(item)
            candidate = [*included, compact_item]
            if len(self._serialize(candidate)) <= budget:
                included.append(compact_item)
        self._record_count(counts, key, len(items), len(included))
        return included

    def _enforce_final_budget(
        self,
        compact_context: dict[str, Any],
        counts: dict[str, dict[str, int]],
        serialized: str,
    ) -> str:
        lists = [
            ('evidence', compact_context['evidence_index']),
            ('log_highlights', compact_context['logs']['highlights']),
            ('metrics', compact_context['metrics']['items']),
            ('node_log_highlights', compact_context['ci_cd_context']['node_log_highlights']),
            ('ansible_task_log_highlights', compact_context['ci_cd_context']['ansible_task_log_highlights']),
            ('failed_nodes', compact_context['ci_cd_context']['failed_nodes']),
            ('approval_records', compact_context['ci_cd_context']['approval_records']),
        ]
        while len(serialized) > self.max_chars:
            populated = [(key, items) for key, items in lists if items]
            if not populated:
                break
            key, items = max(populated, key=lambda pair: len(self._serialize(pair[1][-1])))
            items.pop()
            counts[key]['included'] = max(0, counts[key]['included'] - 1)
            serialized = self._serialize(compact_context)

        if len(serialized) <= self.max_chars:
            return serialized

        minimal = {
            'diagnosis': compact_context.get('diagnosis'),
            'project': compact_context.get('project'),
            'template': compact_context.get('template'),
            'service': compact_context.get('service'),
            'source_alert': compact_context.get('source_alert'),
            'evidence_index': compact_context.get('evidence_index', [])[:5],
            'context_notice': 'Prompt context exceeded the budget and was reduced to core metadata.',
        }
        for key in counts:
            if key != 'evidence':
                counts[key]['included'] = 0
        counts['evidence']['included'] = len(minimal['evidence_index'])
        serialized = self._serialize(minimal)
        while len(serialized) > self.max_chars and minimal['evidence_index']:
            minimal['evidence_index'].pop()
            counts['evidence']['included'] = max(0, counts['evidence']['included'] - 1)
            serialized = self._serialize(minimal)
        if len(serialized) > self.max_chars:
            minimal['diagnosis'] = self._compact_value(minimal.get('diagnosis'), max_string=100)
            minimal['service'] = self._compact_value(minimal.get('service'), max_string=100)
            serialized = self._serialize(minimal)
        if len(serialized) > self.max_chars:
            evidence_refs = [
                item.get('ref')
                for item in compact_context.get('evidence_index', [])
                if item.get('ref')
            ][:10]
            ultra_minimal = {
                'diagnosis': self._identity_fields(compact_context.get('diagnosis'), ('id', 'title')),
                'project': self._identity_fields(compact_context.get('project'), ('id', 'code', 'name')),
                'template': self._identity_fields(compact_context.get('template'), ('id', 'code', 'name')),
                'service': self._identity_fields(compact_context.get('service'), ('id', 'code', 'name')),
                'source_alert': self._identity_fields(
                    compact_context.get('source_alert'),
                    ('id', 'alert_name', 'severity'),
                ),
                'evidence_refs': evidence_refs,
                'context_notice': 'Prompt context exceeded the budget and was reduced to identifiers.',
            }
            serialized = self._serialize(ultra_minimal)
            while len(serialized) > self.max_chars and ultra_minimal['evidence_refs']:
                ultra_minimal['evidence_refs'].pop()
                serialized = self._serialize(ultra_minimal)
            counts['evidence']['included'] = len(ultra_minimal['evidence_refs'])
        return serialized

    @staticmethod
    def _record_count(
        counts: dict[str, dict[str, int]],
        key: str,
        available: int,
        included: int,
    ) -> None:
        counts[key] = {'available': available, 'included': included}

    @staticmethod
    def _evidence_score(item: dict[str, Any]) -> int:
        type_score = {
            'pipeline_node_log': 100,
            'ansible_task_log': 100,
            'alert': 90,
            'pipeline_node': 85,
            'log': 80,
            'metric': 65,
            'ansible_execution': 60,
            'pipeline_run': 55,
            'approval_ticket': 40,
        }.get(item.get('type'), 20)
        raw = item.get('raw') or {}
        severity_score = {
            'critical': 30,
            'error': 20,
            'warning': 10,
        }.get(str(raw.get('severity') or raw.get('level') or '').lower(), 0)
        return type_score + severity_score + min(int(raw.get('score') or 0), 50)

    def _compact_value(
        self,
        value: Any,
        *,
        max_depth: int = 4,
        max_items: int = 12,
        max_string: int = 1000,
        _depth: int = 0,
    ) -> Any:
        if _depth >= max_depth:
            if isinstance(value, (dict, list, tuple)):
                return '[truncated]'
            return self._compact_string(value, max_string)
        if isinstance(value, dict):
            return {
                str(key): self._compact_value(
                    item,
                    max_depth=max_depth,
                    max_items=max_items,
                    max_string=max_string,
                    _depth=_depth + 1,
                )
                for key, item in list(value.items())[:max_items]
                if key != 'raw'
            }
        if isinstance(value, (list, tuple)):
            return [
                self._compact_value(
                    item,
                    max_depth=max_depth,
                    max_items=max_items,
                    max_string=max_string,
                    _depth=_depth + 1,
                )
                for item in list(value)[:max_items]
            ]
        if isinstance(value, str):
            return self._compact_string(value, max_string)
        return value

    @staticmethod
    def _compact_string(value: Any, limit: int) -> Any:
        if value is None or not isinstance(value, str):
            return value
        if len(value) <= limit:
            return value
        return f'{value[:max(0, limit - 14)]}...[truncated]'

    def _identity_fields(self, value: Any, fields: tuple[str, ...]) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        return {
            field: self._compact_string(value.get(field), 100)
            for field in fields
            if value.get(field) is not None
        }

    @staticmethod
    def _serialize(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, default=str, separators=(',', ':'))
