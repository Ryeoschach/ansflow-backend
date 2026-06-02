from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import urljoin

import requests

from .models import ObservabilityDataSource, ObservedService


def _api_url(base_url: str, path: str) -> str:
    return urljoin(base_url.rstrip('/') + '/', path.lstrip('/'))


def _label_selector(labels: dict[str, Any] | None) -> str:
    if not labels:
        return ''
    parts = []
    for key, value in labels.items():
        if value is None or value == '':
            continue
        escaped = str(value).replace('\\', '\\\\').replace('"', '\\"')
        parts.append(f'{key}="{escaped}"')
    return '{' + ','.join(parts) + '}' if parts else ''


class VictoriaClient:
    def __init__(self, datasource: ObservabilityDataSource):
        self.datasource = datasource

    def _request_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {'timeout': self.datasource.timeout_seconds}
        if self.datasource.auth_type == 'bearer' and self.datasource.token:
            kwargs['headers'] = {'Authorization': f'Bearer {self.datasource.token}'}
        elif self.datasource.auth_type == 'basic':
            kwargs['auth'] = (self.datasource.username or '', self.datasource.password or '')
        return kwargs

    def test_connection(self) -> dict[str, Any]:
        if self.datasource.type == 'victoriametrics':
            url = _api_url(self.datasource.base_url, '/api/v1/query')
            response = requests.get(url, params={'query': 'up'}, **self._request_kwargs())
        else:
            url = _api_url(self.datasource.base_url, '/select/logsql/query')
            response = requests.get(url, params={'query': '*', 'limit': 1}, **self._request_kwargs())
        response.raise_for_status()
        return {'ok': True, 'status_code': response.status_code}

    def query_metrics(
        self,
        service: ObservedService,
        start: datetime,
        end: datetime,
        step: str = '60s',
    ) -> list[dict[str, Any]]:
        selector = _label_selector(service.metric_label_selector)
        queries = service.metric_queries or [
            {'name': 'up', 'query': f'up{selector}'},
            {'name': 'cpu_usage', 'query': f'avg(rate(node_cpu_seconds_total{selector}[5m]))'},
            {'name': 'memory_available', 'query': f'node_memory_MemAvailable_bytes{selector}'},
            {'name': 'jvm_heap_used', 'query': f'jvm_memory_used_bytes{selector}'},
            {'name': 'jvm_gc_seconds', 'query': f'rate(jvm_gc_collection_seconds_sum{selector}[5m])'},
        ]
        results = []
        for item in queries:
            query = item.get('query') if isinstance(item, dict) else str(item)
            if not query:
                continue
            url = _api_url(self.datasource.base_url, '/api/v1/query_range')
            response = requests.get(
                url,
                params={
                    'query': query,
                    'start': int(start.timestamp()),
                    'end': int(end.timestamp()),
                    'step': step,
                },
                **self._request_kwargs(),
            )
            response.raise_for_status()
            payload = response.json()
            results.append({
                'name': item.get('name') if isinstance(item, dict) else query,
                'query': query,
                'result': payload.get('data', {}).get('result', []),
            })
        return results

    def query_logs(self, service: ObservedService, start: datetime, end: datetime, limit: int = 200) -> dict[str, Any]:
        selector = _label_selector(service.log_label_selector)
        query = service.log_query or selector or '*'
        url = _api_url(self.datasource.base_url, '/select/logsql/query')
        response = requests.get(
            url,
            params={
                'query': query,
                'start': start.isoformat(),
                'end': end.isoformat(),
                'limit': limit,
            },
            **self._request_kwargs(),
        )
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError:
            payload = {'raw': response.text}
        return {'query': query, 'result': payload}
