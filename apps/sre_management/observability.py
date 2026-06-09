from __future__ import annotations

from datetime import datetime
from typing import Any
from copy import deepcopy
import ipaddress
import socket
from urllib.parse import urljoin
from urllib.parse import urlparse

import requests
from django.conf import settings

from .models import ObservabilityDataSource, ObservedService


class ObservabilityAdapterError(ValueError):
    """Raised when a datasource cannot be queried by the configured adapter."""


BLOCKED_METADATA_IPS = {
    ipaddress.ip_address('169.254.169.254'),
    ipaddress.ip_address('100.100.100.200'),
}


def validate_observability_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname:
        raise ObservabilityAdapterError('Observability datasource URL must use http or https.')
    hostname = parsed.hostname.lower().rstrip('.')
    allowed_hosts = {
        str(item).lower().rstrip('.')
        for item in getattr(settings, 'SRE_OBSERVABILITY_ALLOWED_HOSTS', [])
    }
    if hostname in allowed_hosts:
        return
    try:
        addresses = {
            ipaddress.ip_address(item[4][0])
            for item in socket.getaddrinfo(hostname, parsed.port or 80, type=socket.SOCK_STREAM)
        }
    except (socket.gaierror, ValueError) as exc:
        raise ObservabilityAdapterError(f'Observability datasource host cannot be resolved: {hostname}') from exc
    for address in addresses:
        if address in BLOCKED_METADATA_IPS or address.is_link_local:
            raise ObservabilityAdapterError('Cloud metadata and link-local addresses are not allowed.')
        if (
            address.is_private or address.is_loopback or address.is_reserved
        ) and not getattr(settings, 'SRE_OBSERVABILITY_ALLOW_PRIVATE_NETWORKS', False):
            raise ObservabilityAdapterError(
                f'Private observability host {hostname} is not in SRE_OBSERVABILITY_ALLOWED_HOSTS.',
            )


def _request(method: str, url: str, **kwargs):
    validate_observability_url(url)
    kwargs.setdefault('allow_redirects', False)
    return requests.request(method, url, **kwargs)


def _api_url(base_url: str, path: str) -> str:
    parsed_path = urlparse(path)
    if parsed_path.scheme or parsed_path.netloc:
        raise ObservabilityAdapterError('Observability API path must be relative to the datasource base URL.')
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


def _get_path(payload: Any, path: str | None, default: Any = None) -> Any:
    if not path:
        return default
    current = payload
    for part in path.split('.'):
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit():
            current = current[int(part)]
        else:
            return default
    return current


def _render_template(template: str, context: dict[str, Any]) -> str:
    result = template
    for key, value in context.items():
        result = result.replace('{{ ' + key + ' }}', str(value))
        result = result.replace('{{' + key + '}}', str(value))
    return result


def _template_context(service: ObservedService | None, start: datetime | None = None, end: datetime | None = None, limit: int | None = None) -> dict[str, Any]:
    selector = service.log_label_selector if service else {}
    context: dict[str, Any] = {
        'start': start.isoformat() if start else '',
        'end': end.isoformat() if end else '',
        'limit': limit or '',
        'query': service.log_query if service and service.log_query else '',
        'service.code': service.code if service else '',
        'service.name': service.name if service else '',
        'namespace': service.namespace if service else '',
    }
    context.update({f'label.{key}': value for key, value in (selector or {}).items()})
    return context


def _render_mapping(value: Any, context: dict[str, Any]) -> Any:
    if isinstance(value, str):
        return _render_template(value, context)
    if isinstance(value, dict):
        return {key: _render_mapping(item, context) for key, item in value.items()}
    if isinstance(value, list):
        return [_render_mapping(item, context) for item in value]
    return value


class BaseObservabilityAdapter:
    def __init__(self, datasource: ObservabilityDataSource):
        self.datasource = datasource

    def _request_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {'timeout': self.datasource.timeout_seconds}
        headers: dict[str, str] = {}

        if self.datasource.auth_type == 'bearer' and self.datasource.token:
            headers['Authorization'] = f'Bearer {self.datasource.token}'
        elif self.datasource.auth_type == 'basic':
            kwargs['auth'] = (self.datasource.username or '', self.datasource.password or '')
        elif self.datasource.auth_type == 'header' and self.datasource.query_config.get('headers'):
            headers.update({str(k): str(v) for k, v in self.datasource.query_config.get('headers', {}).items()})
        elif self.datasource.auth_type == 'query':
            pass
        elif self.datasource.auth_type == 'cloud_signature':
            raise ObservabilityAdapterError(
                f"{self.datasource.provider} cloud_signature auth is not implemented yet. "
                "Use bearer/header auth with a proxy or generic_http gateway."
            )

        if headers:
            kwargs['headers'] = headers
        return kwargs

    def _query_params(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        merged = {}
        if self.datasource.auth_type == 'query':
            merged.update({
                str(k): str(v)
                for k, v in (self.datasource.query_config.get('auth_params') or {}).items()
            })
        merged.update(params or {})
        return merged

    def test_connection(self) -> dict[str, Any]:
        raise NotImplementedError

    def query_metrics(
        self,
        service: ObservedService,
        start: datetime,
        end: datetime,
        step: str = '60s',
    ) -> list[dict[str, Any]]:
        raise ObservabilityAdapterError(f"{self.datasource.provider} does not support metric queries")

    def query_logs(self, service: ObservedService, start: datetime, end: datetime, limit: int = 200) -> dict[str, Any]:
        raise ObservabilityAdapterError(f"{self.datasource.provider} does not support log queries")


class VictoriaMetricsAdapter(BaseObservabilityAdapter):
    def test_connection(self) -> dict[str, Any]:
        url = _api_url(self.datasource.base_url, '/api/v1/query')
        response = _request('GET', url, params=self._query_params({'query': 'up'}), **self._request_kwargs())
        response.raise_for_status()
        return {'ok': True, 'status_code': response.status_code, 'provider': self.datasource.provider}

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
            response = _request(
                'GET',
                url,
                params=self._query_params({
                    'query': query,
                    'start': int(start.timestamp()),
                    'end': int(end.timestamp()),
                    'step': step,
                }),
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


class VictoriaLogsAdapter(BaseObservabilityAdapter):
    def test_connection(self) -> dict[str, Any]:
        url = _api_url(self.datasource.base_url, '/select/logsql/query')
        response = _request('GET', url, params=self._query_params({'query': '*', 'limit': 1}), **self._request_kwargs())
        response.raise_for_status()
        return {'ok': True, 'status_code': response.status_code, 'provider': self.datasource.provider}

    def query_logs(self, service: ObservedService, start: datetime, end: datetime, limit: int = 200) -> dict[str, Any]:
        selector = _label_selector(service.log_label_selector)
        query = service.log_query or selector or '*'
        url = _api_url(self.datasource.base_url, '/select/logsql/query')
        response = _request(
            'GET',
            url,
            params=self._query_params({
                'query': query,
                'start': start.isoformat(),
                'end': end.isoformat(),
                'limit': limit,
            }),
            **self._request_kwargs(),
        )
        response.raise_for_status()
        payload = _response_payload(response)
        return _log_result(self.datasource, query, payload)


class LokiLogAdapter(BaseObservabilityAdapter):
    def test_connection(self) -> dict[str, Any]:
        url = _api_url(self.datasource.base_url, '/ready')
        response = _request('GET', url, **self._request_kwargs())
        response.raise_for_status()
        return {'ok': True, 'status_code': response.status_code, 'provider': self.datasource.provider}

    def query_logs(self, service: ObservedService, start: datetime, end: datetime, limit: int = 200) -> dict[str, Any]:
        query = service.log_query or _label_selector(service.log_label_selector) or '{}'
        url = _api_url(self.datasource.base_url, '/loki/api/v1/query_range')
        response = _request(
            'GET',
            url,
            params=self._query_params({
                'query': query,
                'start': int(start.timestamp() * 1_000_000_000),
                'end': int(end.timestamp() * 1_000_000_000),
                'limit': limit,
            }),
            **self._request_kwargs(),
        )
        response.raise_for_status()
        payload = _response_payload(response)
        return _log_result(self.datasource, query, payload)


class ElasticsearchLogAdapter(BaseObservabilityAdapter):
    def test_connection(self) -> dict[str, Any]:
        url = _api_url(self.datasource.base_url, '/')
        response = _request('GET', url, **self._request_kwargs())
        response.raise_for_status()
        return {'ok': True, 'status_code': response.status_code, 'provider': self.datasource.provider}

    def query_logs(self, service: ObservedService, start: datetime, end: datetime, limit: int = 200) -> dict[str, Any]:
        config = self.datasource.query_config or {}
        index = config.get('index') or '_all'
        timestamp_field = (self.datasource.field_mapping or {}).get('timestamp') or '@timestamp'
        query_text = service.log_query or config.get('query') or '*'
        filters = [
            {'range': {timestamp_field: {'gte': start.isoformat(), 'lte': end.isoformat()}}},
        ]
        for key, value in (service.log_label_selector or {}).items():
            if value not in (None, ''):
                filters.append({'term': {key: value}})
        body = {
            'size': limit,
            'sort': [{timestamp_field: {'order': 'asc'}}],
            'query': {
                'bool': {
                    'filter': filters,
                    'must': [{'query_string': {'query': query_text}}],
                }
            },
        }
        url = _api_url(self.datasource.base_url, f'/{index}/_search')
        response = _request('POST', url, json=body, **self._request_kwargs())
        response.raise_for_status()
        payload = _response_payload(response)
        return _log_result(self.datasource, query_text, payload)


class GenericHttpLogAdapter(BaseObservabilityAdapter):
    def test_connection(self) -> dict[str, Any]:
        config = self.datasource.query_config or {}
        path = config.get('health_path') or config.get('path') or '/'
        method = str(config.get('method') or 'GET').upper()
        response = _request(method, _api_url(self.datasource.base_url, path), **self._request_kwargs())
        response.raise_for_status()
        return {'ok': True, 'status_code': response.status_code, 'provider': self.datasource.provider}

    def query_logs(self, service: ObservedService, start: datetime, end: datetime, limit: int = 200) -> dict[str, Any]:
        config = self.datasource.query_config or {}
        method = str(config.get('method') or 'GET').upper()
        path = config.get('path') or '/'
        selector = service.log_label_selector or {}
        template_context = _template_context(service, start, end, limit)
        params = {
            config.get('start_param', 'start'): start.isoformat(),
            config.get('end_param', 'end'): end.isoformat(),
            config.get('limit_param', 'limit'): limit,
        }
        if service.log_query:
            params[config.get('query_param', 'query')] = service.log_query
        params.update(_render_mapping(config.get('params') or {}, template_context))
        body = config.get('body')
        if isinstance(body, (dict, list, str)):
            body = _render_mapping(body, template_context)
        response = _request(
            method,
            _api_url(self.datasource.base_url, path),
            params=self._query_params(params) if method == 'GET' else self._query_params({}),
            json=body if method != 'GET' else None,
            **self._request_kwargs(),
        )
        response.raise_for_status()
        payload = _response_payload(response)
        return _log_result(self.datasource, service.log_query or str(selector), payload)


class AliyunSLSLogAdapter(GenericHttpLogAdapter):
    """HTTP-compatible SLS adapter.

    Direct Aliyun SLS OpenAPI signature is intentionally not embedded here.
    Use bearer/header auth against a gateway or proxy, or add a dedicated
    signer later without changing diagnosis task code.
    """


class TencentCLSLogAdapter(GenericHttpLogAdapter):
    """HTTP-compatible CLS adapter; see AliyunSLSLogAdapter note."""


def _response_payload(response) -> Any:
    try:
        return response.json()
    except ValueError:
        return {'raw': response.text}


def _log_result(datasource: ObservabilityDataSource, query: str, payload: Any) -> dict[str, Any]:
    response_mapping = datasource.response_mapping or {}
    field_mapping = datasource.field_mapping or {}
    items = _get_path(payload, response_mapping.get('items_path'), None)
    if items is None and datasource.provider == 'elasticsearch':
        items = _get_path(payload, 'hits.hits', [])
    elif items is None and datasource.provider == 'loki':
        streams = _get_path(payload, 'data.result', [])
        items = []
        for stream in streams or []:
            labels = stream.get('stream') or {}
            for timestamp, message in stream.get('values') or []:
                items.append({'timestamp': timestamp, 'message': message, 'labels': labels})
    elif items is None:
        items = payload.get('data') if isinstance(payload, dict) and isinstance(payload.get('data'), list) else []

    normalized = [_normalize_log_item(item, field_mapping) for item in (items or [])[:200]]
    return {
        'datasource': {
            'id': datasource.id,
            'name': datasource.name,
            'kind': datasource.kind,
            'provider': datasource.provider,
        },
        'query': query,
        'items': normalized,
    }


def _normalize_log_item(item: Any, field_mapping: dict[str, str]) -> dict[str, Any]:
    source = item.get('_source') if isinstance(item, dict) and isinstance(item.get('_source'), dict) else item
    if not isinstance(source, dict):
        return {'timestamp': None, 'level': None, 'message': str(source), 'service': None, 'instance': None, 'labels': {}}
    return {
        'timestamp': _get_path(source, field_mapping.get('timestamp')) or source.get('timestamp') or source.get('@timestamp'),
        'level': _get_path(source, field_mapping.get('level')) or source.get('level') or source.get('severity'),
        'message': _get_path(source, field_mapping.get('message')) or source.get('message') or source.get('log') or source.get('raw'),
        'service': _get_path(source, field_mapping.get('service')) or source.get('service') or source.get('app'),
        'instance': _get_path(source, field_mapping.get('instance')) or source.get('instance') or source.get('host') or source.get('pod'),
        'labels': _get_path(source, field_mapping.get('labels')) or source.get('labels') or {},
    }


DATASOURCE_CAPABILITIES = {
    'victoriametrics': {
        'label': 'VictoriaMetrics',
        'kind': 'metric',
        'supports_metrics': True,
        'supports_logs': False,
        'auth_types': ['none', 'bearer', 'basic', 'header', 'query'],
        'default_base_url': 'http://victoriametrics:8428',
        'query_config': {},
        'field_mapping': {},
        'response_mapping': {},
        'notes': 'PromQL query_range compatible datasource.',
    },
    'victorialogs': {
        'label': 'VictoriaLogs',
        'kind': 'log',
        'supports_metrics': False,
        'supports_logs': True,
        'auth_types': ['none', 'bearer', 'basic', 'header', 'query'],
        'default_base_url': 'http://victorialogs:9428',
        'query_config': {},
        'field_mapping': {
            'timestamp': '_time',
            'level': 'level',
            'message': '_msg',
            'service': 'service',
            'instance': 'instance',
            'labels': 'labels',
        },
        'response_mapping': {'items_path': 'data'},
        'notes': 'Uses /select/logsql/query. Service log_query can override generated label selector.',
    },
    'elasticsearch': {
        'label': 'Elasticsearch',
        'kind': 'log',
        'supports_metrics': False,
        'supports_logs': True,
        'auth_types': ['none', 'bearer', 'basic', 'header', 'query'],
        'default_base_url': 'http://elasticsearch:9200',
        'query_config': {'index': 'logs-*', 'query': '*'},
        'field_mapping': {
            'timestamp': '@timestamp',
            'level': 'level',
            'message': 'message',
            'service': 'service.name',
            'instance': 'host.name',
            'labels': 'labels',
        },
        'response_mapping': {'items_path': 'hits.hits'},
        'notes': 'Queries /{index}/_search and normalizes hits._source fields.',
    },
    'loki': {
        'label': 'Loki',
        'kind': 'log',
        'supports_metrics': False,
        'supports_logs': True,
        'auth_types': ['none', 'bearer', 'basic', 'header', 'query'],
        'default_base_url': 'http://loki:3100',
        'query_config': {},
        'field_mapping': {
            'timestamp': 'timestamp',
            'message': 'message',
            'service': 'labels.service',
            'instance': 'labels.instance',
            'labels': 'labels',
        },
        'response_mapping': {},
        'notes': 'Uses /loki/api/v1/query_range. log_label_selector is rendered as a LogQL selector.',
    },
    'generic_http': {
        'label': 'Generic HTTP',
        'kind': 'log',
        'supports_metrics': False,
        'supports_logs': True,
        'auth_types': ['none', 'bearer', 'basic', 'header', 'query'],
        'default_base_url': 'http://logs-gateway:8080',
        'query_config': {
            'method': 'GET',
            'path': '/logs/search',
            'health_path': '/health',
            'query_param': 'query',
            'start_param': 'start',
            'end_param': 'end',
            'limit_param': 'limit',
            'params': {'service': '{{service.code}}'},
        },
        'field_mapping': {
            'timestamp': 'timestamp',
            'level': 'level',
            'message': 'message',
            'service': 'service',
            'instance': 'instance',
            'labels': 'labels',
        },
        'response_mapping': {'items_path': 'data.items'},
        'notes': 'For log gateways, cloud log proxies, or any JSON API. Supports {{start}}, {{end}}, {{limit}}, {{query}}, {{service.code}}, {{label.xxx}} templates.',
    },
    'aliyun_sls': {
        'label': 'Aliyun SLS',
        'kind': 'log',
        'supports_metrics': False,
        'supports_logs': True,
        'auth_types': ['bearer', 'header', 'query'],
        'default_base_url': 'https://sls-proxy.example.com',
        'query_config': {
            'method': 'GET',
            'path': '/logs/search',
            'health_path': '/health',
            'query_param': 'query',
            'start_param': 'from',
            'end_param': 'to',
            'limit_param': 'line',
            'params': {'project': '{{label.project}}', 'logstore': '{{label.logstore}}'},
        },
        'field_mapping': {
            'timestamp': '__time__',
            'level': 'level',
            'message': 'message',
            'service': 'service',
            'instance': 'instance',
            'labels': 'labels',
        },
        'response_mapping': {'items_path': 'data.items'},
        'notes': 'Use an SLS proxy or gateway that handles Aliyun signature. Direct cloud_signature auth is reserved for a later signer plugin.',
    },
    'tencent_cls': {
        'label': 'Tencent CLS',
        'kind': 'log',
        'supports_metrics': False,
        'supports_logs': True,
        'auth_types': ['bearer', 'header', 'query'],
        'default_base_url': 'https://cls-proxy.example.com',
        'query_config': {
            'method': 'GET',
            'path': '/logs/search',
            'health_path': '/health',
            'query_param': 'query',
            'start_param': 'start_time',
            'end_param': 'end_time',
            'limit_param': 'limit',
            'params': {'topic_id': '{{label.topic_id}}'},
        },
        'field_mapping': {
            'timestamp': 'time',
            'level': 'level',
            'message': 'content',
            'service': 'service',
            'instance': 'instance',
            'labels': 'labels',
        },
        'response_mapping': {'items_path': 'data.results'},
        'notes': 'Use a CLS proxy or gateway that handles Tencent Cloud signature. Direct cloud_signature auth is reserved for a later signer plugin.',
    },
}


def get_datasource_capabilities() -> dict[str, dict[str, Any]]:
    return deepcopy(DATASOURCE_CAPABILITIES)


METRIC_ADAPTERS = {
    'victoriametrics': VictoriaMetricsAdapter,
}

LOG_ADAPTERS = {
    'victorialogs': VictoriaLogsAdapter,
    'elasticsearch': ElasticsearchLogAdapter,
    'loki': LokiLogAdapter,
    'generic_http': GenericHttpLogAdapter,
    'aliyun_sls': AliyunSLSLogAdapter,
    'tencent_cls': TencentCLSLogAdapter,
}


def get_metric_adapter(datasource: ObservabilityDataSource) -> BaseObservabilityAdapter:
    adapter_cls = METRIC_ADAPTERS.get(datasource.provider or datasource.type)
    if not adapter_cls:
        raise ObservabilityAdapterError(f"Unsupported metric datasource provider: {datasource.provider}")
    return adapter_cls(datasource)


def get_log_adapter(datasource: ObservabilityDataSource) -> BaseObservabilityAdapter:
    adapter_cls = LOG_ADAPTERS.get(datasource.provider or datasource.type)
    if not adapter_cls:
        raise ObservabilityAdapterError(f"Unsupported log datasource provider: {datasource.provider}")
    return adapter_cls(datasource)


def get_observability_adapter(datasource: ObservabilityDataSource) -> BaseObservabilityAdapter:
    if datasource.kind == 'metric':
        return get_metric_adapter(datasource)
    if datasource.kind == 'log':
        return get_log_adapter(datasource)
    raise ObservabilityAdapterError(f"Unsupported datasource kind: {datasource.kind}")


class VictoriaClient:
    """Compatibility wrapper for existing imports and tests."""

    def __init__(self, datasource: ObservabilityDataSource):
        self.datasource = datasource

    def _adapter(self) -> BaseObservabilityAdapter:
        if self.datasource.provider == 'victorialogs' or self.datasource.type == 'victorialogs':
            return VictoriaLogsAdapter(self.datasource)
        return VictoriaMetricsAdapter(self.datasource)

    def test_connection(self) -> dict[str, Any]:
        return self._adapter().test_connection()

    def query_metrics(
        self,
        service: ObservedService,
        start: datetime,
        end: datetime,
        step: str = '60s',
    ) -> list[dict[str, Any]]:
        return VictoriaMetricsAdapter(self.datasource).query_metrics(service, start, end, step=step)

    def query_logs(self, service: ObservedService, start: datetime, end: datetime, limit: int = 200) -> dict[str, Any]:
        return VictoriaLogsAdapter(self.datasource).query_logs(service, start, end, limit=limit)
