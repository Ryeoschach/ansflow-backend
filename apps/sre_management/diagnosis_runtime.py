from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Callable

from django.conf import settings
from django.db import close_old_connections, connection
from django.db.models import Q
from django.utils.module_loading import import_string

from .diagnosis_security import redact_sensitive_data


@dataclass
class CollectorSpec:
    key: str
    collect: Callable[[], dict[str, Any]]
    enabled: bool = True


class DiagnosisCollectorManager:
    """Runs independent context collectors with a uniform result contract."""

    def run(self, specs: list[CollectorSpec]) -> dict[str, dict[str, Any]]:
        enabled = [spec for spec in specs if spec.enabled]
        if not enabled:
            return {}
        workers = int(getattr(settings, 'SRE_DIAGNOSIS_COLLECTOR_WORKERS', 4))
        if connection.vendor == 'sqlite':
            workers = 1
        if workers <= 1:
            return {spec.key: self._run_one(spec) for spec in enabled}
        outcomes = {}
        with ThreadPoolExecutor(max_workers=min(workers, len(enabled))) as executor:
            futures = {executor.submit(self._run_one, spec): spec.key for spec in enabled}
            for future in as_completed(futures):
                outcomes[futures[future]] = future.result()
        return outcomes

    def _run_one(self, spec: CollectorSpec) -> dict[str, Any]:
        started = time.monotonic()
        close_old_connections()
        try:
            data = redact_sensitive_data(spec.collect())
            return {
                'status': 'success',
                'data': data,
                'count': self._count(data),
                'duration_ms': round((time.monotonic() - started) * 1000, 2),
            }
        except Exception as exc:
            return {
                'status': 'failed',
                'data': {},
                'count': 0,
                'error': str(exc),
                'duration_ms': round((time.monotonic() - started) * 1000, 2),
            }
        finally:
            close_old_connections()

    def _count(self, value: Any) -> int:
        if isinstance(value, list):
            return len(value)
        if isinstance(value, dict):
            return sum(len(item) if isinstance(item, list) else int(bool(item)) for item in value.values())
        return int(bool(value))


def build_external_collector_specs(run, start, end, template_snapshot) -> list[CollectorSpec]:
    """Loads optional read-only collectors declared by dotted path in settings."""

    specs = []
    for dotted_path in getattr(settings, 'SRE_DIAGNOSIS_EXTRA_COLLECTORS', []):
        try:
            collector_factory = import_string(dotted_path)
        except Exception as exc:
            specs.append(CollectorSpec(
                key=dotted_path.rsplit('.', 1)[-1],
                collect=lambda exc=exc: _raise_collector_error(exc),
            ))
            continue
        collector = collector_factory() if isinstance(collector_factory, type) else collector_factory
        key = getattr(collector, 'key', dotted_path.rsplit('.', 1)[-1])
        enabled = getattr(collector, 'enabled', True)
        if callable(enabled):
            enabled = enabled(run, template_snapshot)
        specs.append(CollectorSpec(
            key=key,
            enabled=bool(enabled),
            collect=lambda collector=collector: collector.collect(
                run,
                start,
                end,
                template_snapshot,
            ),
        ))
    return specs


def _raise_collector_error(exc):
    raise RuntimeError(f'Failed to load diagnosis collector: {exc}') from exc


class RuntimeAssetCollector:
    """Read-only runtime context collector. It never executes shell commands."""

    def collect(self, run, start, end, template_snapshot) -> dict[str, Any]:
        from apps.host_management.models import Host
        from apps.k8s_management.models import K8sCluster

        query_params = run.query_params or {}
        service = run.service
        host_id = query_params.get('host_id')
        cluster_id = query_params.get('k8s_cluster_id') or getattr(service, 'k8s_cluster_id', None)
        namespace = query_params.get('namespace') or getattr(service, 'namespace', None)
        context = {
            'target': {
                'host_id': host_id,
                'k8s_cluster_id': cluster_id,
                'namespace': namespace,
                'workload_kind': query_params.get('workload_kind'),
                'workload_name': query_params.get('workload_name'),
                'jvm_instance': query_params.get('jvm_instance'),
            },
            'hosts': [],
            'cluster': None,
            'pods': [],
            'deployments': [],
            'k8s_events': [],
            'pod_metrics': [],
        }
        hosts = Host.objects.filter(project_id=run.project_id)
        if host_id:
            hosts = hosts.filter(id=host_id)
        elif service:
            service_filter = Q(observed_services=service)
            if service.resource_pool_id:
                service_filter |= Q(pools=service.resource_pool_id)
            if service.environment_id:
                service_filter |= Q(env_id=service.environment_id)
            hosts = hosts.filter(service_filter).distinct()
        context['hosts'] = list(hosts.values(
            'id', 'hostname', 'private_ip', 'ip_address', 'os_type',
            'cpu', 'memory', 'disk', 'status', 'env__name',
        )[:100])
        if not cluster_id:
            return context
        cluster = K8sCluster.objects.filter(id=cluster_id, project_id=run.project_id).first()
        if not cluster:
            return context
        context['cluster'] = {
            'id': cluster.id,
            'name': cluster.name,
            'status': cluster.status,
            'version': cluster.version,
            'node_count': cluster.node_count,
            'ready_node_count': cluster.ready_node_count,
            'cpu_capacity': cluster.cpu_capacity,
            'memory_capacity': cluster.memory_capacity,
            'last_seen': cluster.last_seen,
            'error_message': cluster.error_message,
        }
        collection = ((template_snapshot or {}).get('content') or {}).get('context_collection') or {}
        if not collection.get('k8s_runtime', False):
            return context
        return self._collect_k8s_live(
            cluster,
            context,
            namespace,
            start,
            end,
            query_params.get('workload_kind'),
            query_params.get('workload_name'),
        )

    def _collect_k8s_live(
        self,
        cluster,
        context,
        namespace,
        start,
        end,
        workload_kind=None,
        workload_name=None,
    ):
        from kubernetes import client as k8s_client
        from apps.k8s_management.utils.k8s_helper import get_k8s_client

        api_client = get_k8s_client(cluster)
        core_api = k8s_client.CoreV1Api(api_client)
        apps_api = k8s_client.AppsV1Api(api_client)
        timeout = int(getattr(settings, 'SRE_DIAGNOSIS_K8S_TIMEOUT_SECONDS', 15))
        request_timeout = (5, timeout)
        pods = (
            core_api.list_namespaced_pod(namespace, _request_timeout=request_timeout).items
            if namespace
            else core_api.list_pod_for_all_namespaces(_request_timeout=request_timeout).items
        )
        if workload_name:
            pods = [
                pod for pod in pods
                if workload_name in pod.metadata.name
                or any(
                    owner.name == workload_name
                    for owner in (pod.metadata.owner_references or [])
                )
            ]
        context['pods'] = [{
            'name': pod.metadata.name,
            'namespace': pod.metadata.namespace,
            'status': pod.status.phase,
            'pod_ip': pod.status.pod_ip,
            'node_name': pod.spec.node_name,
            'restarts': sum(item.restart_count for item in (pod.status.container_statuses or [])),
            'creation_timestamp': pod.metadata.creation_timestamp,
        } for pod in pods[:200]]
        deployments = (
            apps_api.list_namespaced_deployment(namespace, _request_timeout=request_timeout).items
            if namespace
            else apps_api.list_deployment_for_all_namespaces(_request_timeout=request_timeout).items
        )
        if workload_name and str(workload_kind or '').lower() in {'deployment', 'deploy', ''}:
            deployments = [item for item in deployments if item.metadata.name == workload_name]
        context['deployments'] = [{
            'name': item.metadata.name,
            'namespace': item.metadata.namespace,
            'desired_replicas': item.spec.replicas,
            'available_replicas': item.status.available_replicas or 0,
            'ready_replicas': item.status.ready_replicas or 0,
            'updated_replicas': item.status.updated_replicas or 0,
        } for item in deployments[:100]]
        events = (
            core_api.list_namespaced_event(namespace, _request_timeout=request_timeout).items
            if namespace
            else core_api.list_event_for_all_namespaces(_request_timeout=request_timeout).items
        )
        context['k8s_events'] = [{
            'reason': event.reason,
            'message': event.message,
            'type': event.type,
            'object': f'{event.involved_object.kind}/{event.involved_object.name}',
            'namespace': event.metadata.namespace,
            'count': event.count,
            'first_timestamp': event.first_timestamp,
            'last_timestamp': event.last_timestamp,
        } for event in events if (event.last_timestamp or event.first_timestamp) and (
            start <= (event.last_timestamp or event.first_timestamp) <= end
        )][:100]
        try:
            custom_api = k8s_client.CustomObjectsApi(api_client)
            metrics = (
                custom_api.list_namespaced_custom_object(
                    'metrics.k8s.io',
                    'v1beta1',
                    namespace,
                    'pods',
                    _request_timeout=request_timeout,
                )
                if namespace
                else custom_api.list_cluster_custom_object(
                    'metrics.k8s.io',
                    'v1beta1',
                    'pods',
                    _request_timeout=request_timeout,
                )
            )
            context['pod_metrics'] = (metrics or {}).get('items', [])[:200]
        except Exception as exc:
            context['metrics_warning'] = str(exc)
        return context
