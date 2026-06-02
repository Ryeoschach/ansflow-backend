from __future__ import annotations

from string import Template


ALERT_RULE_TEMPLATES = [
    {
        'id': 'host_cpu_high',
        'name': '主机 CPU 使用率过高',
        'category': 'host',
        'description': '基于 node_exporter 的 CPU 使用率告警。',
        'variables': {'job': 'node-exporter', 'threshold': '85', 'for': '5m', 'severity': 'warning'},
        'template': '''
groups:
  - name: ansflow-host
    rules:
      - alert: HostCPUUsageHigh
        expr: 100 - (avg by(instance) (rate(node_cpu_seconds_total{job="$job",mode="idle"}[5m])) * 100) > $threshold
        for: $for
        labels:
          severity: $severity
          source: vmalert
        annotations:
          summary: "Host CPU usage is high"
          description: "Instance {{ $labels.instance }} CPU usage is above $threshold%."
''',
    },
    {
        'id': 'host_memory_high',
        'name': '主机内存使用率过高',
        'category': 'host',
        'description': '基于 node_exporter 的内存使用率告警。',
        'variables': {'job': 'node-exporter', 'threshold': '90', 'for': '5m', 'severity': 'warning'},
        'template': '''
groups:
  - name: ansflow-host
    rules:
      - alert: HostMemoryUsageHigh
        expr: (1 - node_memory_MemAvailable_bytes{job="$job"} / node_memory_MemTotal_bytes{job="$job"}) * 100 > $threshold
        for: $for
        labels:
          severity: $severity
          source: vmalert
        annotations:
          summary: "Host memory usage is high"
          description: "Instance {{ $labels.instance }} memory usage is above $threshold%."
''',
    },
    {
        'id': 'host_disk_high',
        'name': '主机磁盘使用率过高',
        'category': 'host',
        'description': '基于 node_exporter 的文件系统使用率告警。',
        'variables': {'job': 'node-exporter', 'mountpoint': '/', 'threshold': '85', 'for': '10m', 'severity': 'warning'},
        'template': '''
groups:
  - name: ansflow-host
    rules:
      - alert: HostDiskUsageHigh
        expr: (1 - node_filesystem_avail_bytes{job="$job",mountpoint="$mountpoint",fstype!~"tmpfs|overlay"} / node_filesystem_size_bytes{job="$job",mountpoint="$mountpoint",fstype!~"tmpfs|overlay"}) * 100 > $threshold
        for: $for
        labels:
          severity: $severity
          source: vmalert
        annotations:
          summary: "Host disk usage is high"
          description: "Instance {{ $labels.instance }} mount $mountpoint usage is above $threshold%."
''',
    },
    {
        'id': 'service_down',
        'name': '服务存活异常',
        'category': 'service',
        'description': '基于 up 指标的服务存活告警。',
        'variables': {'job': 'app', 'for': '3m', 'severity': 'critical'},
        'template': '''
groups:
  - name: ansflow-service
    rules:
      - alert: ServiceDown
        expr: up{job="$job"} == 0
        for: $for
        labels:
          severity: $severity
          source: vmalert
        annotations:
          summary: "Service is down"
          description: "Target {{ $labels.instance }} of job $job is down."
''',
    },
    {
        'id': 'k8s_pod_restarts',
        'name': 'K8s Pod 频繁重启',
        'category': 'k8s',
        'description': '基于 kube-state-metrics 的 Pod 重启告警。',
        'variables': {'namespace': 'default', 'threshold': '3', 'for': '5m', 'severity': 'warning'},
        'template': '''
groups:
  - name: ansflow-k8s
    rules:
      - alert: K8sPodRestartingTooOften
        expr: increase(kube_pod_container_status_restarts_total{namespace="$namespace"}[10m]) > $threshold
        for: $for
        labels:
          severity: $severity
          source: vmalert
        annotations:
          summary: "Pod restarts too often"
          description: "Pod {{ $labels.pod }} container {{ $labels.container }} restarted more than $threshold times."
''',
    },
    {
        'id': 'jvm_heap_high',
        'name': 'JVM 堆内存使用率过高',
        'category': 'jvm',
        'description': '基于 jmx_exporter 的 JVM 堆内存告警。',
        'variables': {'job': 'java-app', 'threshold': '85', 'for': '5m', 'severity': 'warning'},
        'template': '''
groups:
  - name: ansflow-jvm
    rules:
      - alert: JVMHeapUsageHigh
        expr: sum by(instance) (jvm_memory_used_bytes{job="$job",area="heap"}) / sum by(instance) (jvm_memory_max_bytes{job="$job",area="heap"}) * 100 > $threshold
        for: $for
        labels:
          severity: $severity
          source: vmalert
        annotations:
          summary: "JVM heap usage is high"
          description: "Instance {{ $labels.instance }} JVM heap usage is above $threshold%."
''',
    },
    {
        'id': 'jvm_gc_slow',
        'name': 'JVM GC 耗时过高',
        'category': 'jvm',
        'description': '基于 jmx_exporter 的 GC 耗时告警。',
        'variables': {'job': 'java-app', 'threshold': '1', 'for': '5m', 'severity': 'warning'},
        'template': '''
groups:
  - name: ansflow-jvm
    rules:
      - alert: JVMGCSlow
        expr: rate(jvm_gc_collection_seconds_sum{job="$job"}[5m]) > $threshold
        for: $for
        labels:
          severity: $severity
          source: vmalert
        annotations:
          summary: "JVM GC is slow"
          description: "Instance {{ $labels.instance }} GC seconds rate is above $threshold."
''',
    },
]


def list_templates():
    return [
        {key: value for key, value in item.items() if key != 'template'}
        for item in ALERT_RULE_TEMPLATES
    ]


def render_template(template_id: str, variables: dict | None = None) -> dict:
    item = next((tpl for tpl in ALERT_RULE_TEMPLATES if tpl['id'] == template_id), None)
    if not item:
        raise KeyError(template_id)
    merged = {**item['variables'], **(variables or {})}
    yaml = Template(item['template'].strip()).safe_substitute(merged)
    return {
        'id': item['id'],
        'name': item['name'],
        'yaml': yaml,
        'variables': merged,
    }
