import logging
import os
from celery import shared_task
from .models import AlertEvent, DiagnosisRun, DiagnosisTemplate, ObservabilityDataSource, SelfHealingPolicy
from apps.ai_engine.rag_service import RAGService
from django.utils import timezone

# Fix for macOS Celery fork safety
os.environ["OBJC_DISABLE_INITIALIZE_FORK_SAFETY"] = "YES"
# Disable parallel tokenizers to avoid SIGSEGV in forked processes
os.environ["TOKENIZERS_PARALLELISM"] = "false"

logger = logging.getLogger(__name__)


def _template_snapshot_for_run(run):
    query_params = run.query_params or {}
    snapshot = query_params.get('template_snapshot')
    if snapshot:
        return snapshot
    if run.template_id:
        template = DiagnosisTemplate.objects.filter(id=run.template_id).first()
        if template:
            snapshot = template.to_snapshot()
            query_params['template_snapshot'] = snapshot
            run.query_params = query_params
            run.save(update_fields=['query_params'])
            return snapshot
    return None


def _template_collection_config(template_snapshot):
    content = (template_snapshot or {}).get('content') or {}
    return content.get('context_collection') or {}


def _template_log_keywords(template_snapshot):
    content = (template_snapshot or {}).get('content') or {}
    keywords = content.get('log_keywords') or []
    return [str(item).lower() for item in keywords if str(item).strip()]


def _template_log_datasource_ids(template_snapshot):
    content = (template_snapshot or {}).get('content') or {}
    raw_ids = content.get('log_datasource_ids') or content.get('log_sources') or []
    ids = []
    for item in raw_ids:
        value = item.get('id') if isinstance(item, dict) else item
        try:
            ids.append(int(value))
        except (TypeError, ValueError):
            continue
    return ids


def _template_metric_datasource_ids(template_snapshot):
    content = (template_snapshot or {}).get('content') or {}
    raw_ids = content.get('metric_datasource_ids') or content.get('metric_sources') or []
    ids = []
    for item in raw_ids:
        value = item.get('id') if isinstance(item, dict) else item
        try:
            ids.append(int(value))
        except (TypeError, ValueError):
            continue
    return ids


def _select_metric_datasources(service, template_snapshot):
    datasource_ids = _template_metric_datasource_ids(template_snapshot)
    if datasource_ids:
        return list(ObservabilityDataSource.objects.filter(
            id__in=datasource_ids,
            kind='metric',
            is_active=True,
        ).order_by('-is_default', 'id'))

    selected = []
    seen = set()
    if service and service.metric_datasource_id and service.metric_datasource and service.metric_datasource.is_active:
        selected.append(service.metric_datasource)
        seen.add(service.metric_datasource_id)

    defaults = ObservabilityDataSource.objects.filter(kind='metric', is_default=True, is_active=True).order_by('id')
    for datasource in defaults:
        if datasource.id in seen:
            continue
        selected.append(datasource)
        seen.add(datasource.id)
    return selected


def _select_log_datasources(service, template_snapshot):
    datasource_ids = _template_log_datasource_ids(template_snapshot)
    if datasource_ids:
        return list(ObservabilityDataSource.objects.filter(
            id__in=datasource_ids,
            kind='log',
            is_active=True,
        ).order_by('-is_default', 'id'))

    selected = []
    seen = set()
    if service and service.log_datasource_id and service.log_datasource and service.log_datasource.is_active:
        selected.append(service.log_datasource)
        seen.add(service.log_datasource_id)

    defaults = ObservabilityDataSource.objects.filter(kind='log', is_default=True, is_active=True).order_by('id')
    for datasource in defaults:
        if datasource.id in seen:
            continue
        selected.append(datasource)
        seen.add(datasource.id)
    return selected


def _normalize_metric_context(datasource, metrics, start, end):
    normalized_metrics = []
    for index, item in enumerate(metrics or [], start=1):
        metric_name = item.get('name') or item.get('query') or f'metric_{index}'
        normalized_metrics.append({
            **item,
            'datasource': {
                'id': datasource.id,
                'name': datasource.name,
                'provider': datasource.provider,
            },
            'evidence_id': f'metric:{datasource.id}:{metric_name}',
        })
    return {
        'datasource': {
            'id': datasource.id,
            'name': datasource.name,
            'kind': datasource.kind,
            'provider': datasource.provider,
        },
        'time_range': {
            'start': start.isoformat(),
            'end': end.isoformat(),
        },
        'metrics': normalized_metrics,
        'count': len(normalized_metrics),
    }


def _normalize_log_context(datasource, logs, highlights, start, end):
    items = logs.get('items') if isinstance(logs, dict) else []
    query = logs.get('query') if isinstance(logs, dict) else None
    normalized_highlights = []
    for index, item in enumerate(highlights or [], start=1):
        enriched = {
            **item,
            'datasource': {
                'id': datasource.id,
                'name': datasource.name,
                'provider': datasource.provider,
            },
            'evidence_id': f'log:{datasource.id}:{index}',
        }
        normalized_highlights.append(enriched)
    return {
        'datasource': {
            'id': datasource.id,
            'name': datasource.name,
            'kind': datasource.kind,
            'provider': datasource.provider,
        },
        'query': query,
        'time_range': {
            'start': start.isoformat(),
            'end': end.isoformat(),
        },
        'items': items or [],
        'count': len(items or []),
        'highlights': normalized_highlights,
        'highlight_count': len(normalized_highlights),
    }


def _highlight_text_lines(text, keywords, limit=30):
    if not text:
        return []
    keywords = keywords or ['error', 'failed', 'exception', 'timeout']
    highlights = []
    for line_no, line in enumerate(str(text).splitlines(), start=1):
        lower = line.lower()
        matched = [keyword for keyword in keywords if keyword in lower]
        if matched:
            highlights.append({'line_no': line_no, 'line': line[:1000], 'matched_keywords': matched})
        if len(highlights) >= limit:
            break
    return highlights


def _collect_ci_cd_context(run, start, end, template_snapshot):
    from apps.approval_center.models import ApprovalTicket
    from apps.pipeline_management.models import PipelineNodeRun, PipelineRun
    from apps.task_management.models import AnsibleExecution, TaskLog
    from django.db.models import Q

    query_params = run.query_params or {}
    collection = _template_collection_config(template_snapshot)
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
        'collection_summary': {
            'pipeline_run': {'status': 'skipped', 'count': 0},
            'failed_nodes': {'status': 'skipped', 'count': 0},
            'node_logs': {'status': 'skipped', 'count': 0},
            'ansible_execution': {'status': 'skipped', 'count': 0},
            'ansible_task_logs': {'status': 'skipped', 'count': 0},
            'approval_records': {'status': 'skipped', 'count': 0},
        },
    }

    pipeline_run = None
    pipeline_run_id = query_params.get('pipeline_run_id')
    node_run_id = query_params.get('pipeline_node_run_id')
    ansible_execution_id = query_params.get('ansible_execution_id')
    if pipeline_run_id:
        pipeline_run = PipelineRun.objects.select_related('pipeline').filter(id=pipeline_run_id).first()
    elif node_run_id:
        node_run = PipelineNodeRun.objects.select_related('run', 'run__pipeline').filter(id=node_run_id).first()
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
            'extra_vars': pipeline_run.extra_vars,
        }
        context['collection_summary']['pipeline_run'] = {'status': 'success', 'count': 1}

    if collection.get('failed_nodes', True):
        node_queryset = PipelineNodeRun.objects.all()
        if node_run_id:
            node_queryset = node_queryset.filter(id=node_run_id)
        elif pipeline_run:
            node_queryset = node_queryset.filter(run=pipeline_run, status='failed')
        else:
            node_queryset = node_queryset.filter(create_time__range=(start, end), status='failed')
        failed_nodes = list(node_queryset.values(
            'id', 'run_id', 'node_id', 'node_type', 'node_label', 'status',
            'approval_time', 'approval_comment', 'start_time', 'end_time', 'create_time', 'output_data',
        )[:20])
        context['failed_nodes'] = failed_nodes
        context['collection_summary']['failed_nodes'] = {'status': 'success', 'count': len(failed_nodes)}

        if collection.get('node_logs', True):
            log_nodes = PipelineNodeRun.objects.filter(id__in=[item['id'] for item in failed_nodes])
            for node in log_nodes:
                for item in _highlight_text_lines(node.logs, keywords, limit=10):
                    item.update({'node_run_id': node.id, 'node_id': node.node_id, 'node_label': node.node_label})
                    context['node_log_highlights'].append(item)
            context['collection_summary']['node_logs'] = {'status': 'success', 'count': len(context['node_log_highlights'])}

    if collection.get('ansible_execution') and ansible_execution_id:
        execution = AnsibleExecution.objects.select_related('task').filter(id=ansible_execution_id).first()
        if execution:
            context['ansible_execution'] = {
                'id': execution.id,
                'task_id': execution.task_id,
                'task_name': getattr(execution.task, 'name', None),
                'status': execution.status,
                'result_summary': execution.result_summary,
                'extra_vars_snapshot': execution.extra_vars_snapshot,
                'start_time': execution.start_time,
                'end_time': execution.end_time,
                'create_time': execution.create_time,
            }
            context['collection_summary']['ansible_execution'] = {'status': 'success', 'count': 1}
            if collection.get('ansible_task_logs', True):
                logs = list(TaskLog.objects.filter(execution=execution).values('id', 'host', 'output', 'create_time')[:50])
                context['ansible_task_logs'] = logs
                for log in logs:
                    for item in _highlight_text_lines(log.get('output'), keywords, limit=5):
                        item.update({'id': log.get('id'), 'host': log.get('host'), 'create_time': log.get('create_time')})
                        context['ansible_task_log_highlights'].append(item)
                context['collection_summary']['ansible_task_logs'] = {'status': 'success', 'count': len(logs)}

    if collection.get('approval_records', True):
        approvals = ApprovalTicket.objects.filter(create_time__range=(start, end))
        if pipeline_run:
            approvals = approvals.filter(Q(target_id=str(pipeline_run.id)) | Q(title__icontains=str(pipeline_run.id)))
        context['approval_records'] = list(approvals.values('id', 'title', 'status', 'resource_type', 'target_id', 'create_time', 'audit_time')[:20])
        context['collection_summary']['approval_records'] = {'status': 'success', 'count': len(context['approval_records'])}

    return context

@shared_task(name="apps.sre_management.tasks.analyze_alert_event", bind=True, max_retries=3)
def analyze_alert_event(self, alert_id):
    """
    异步 AI 分析告警事件并匹配自愈策略
    """
    print(f"\n!!! [CELERY EXEC] Starting analyze_alert_event for ID: {alert_id} !!!\n")
    logger.info(f"[SRE] Starting AI analysis for alert_id: {alert_id}")
    alert = None
    try:
        alert = AlertEvent.objects.get(id=alert_id)
        alert.healing_status = 'analyzing'
        alert.save()
        logger.info(f"[SRE] Alert {alert_id} status set to analyzing")

        # 1. 构造 RAG 查询问题
        labels_str = ", ".join([f"{k}={v}" for k, v in alert.labels.items()])
        question = f"我收到了一个名为 '{alert.alert_name}' 的告警，严重程度为 '{alert.severity}'。标签信息: {labels_str}。请根据历史经验给出诊断建议。"
        logger.info(f"[SRE] Constructing question: {question}")

        # 2. 调用 RAG 引擎
        logger.info("[SRE] Initializing RAGService...")
        rag_service = RAGService()
        logger.info("[SRE] RAGService initialized successfully")
        
        analysis_result = ""
        # 使用同步方式获取全部结果
        logger.info("[SRE] Invoking RAG chain...")
        chain = rag_service.get_chat_chain()
        analysis_result = chain.invoke(question)
        logger.info(f"[SRE] AI Analysis result received: {analysis_result[:100]}...")

        # 2.1 [优化] 增加告警降噪分析
        recent_same_alerts = AlertEvent.objects.filter(
            fingerprint=alert.fingerprint,
            create_time__gte=timezone.now() - timezone.timedelta(hours=1)
        ).exclude(id=alert.id).count()
        
        if recent_same_alerts > 3:
            noise_reduction_msg = f"\n\n### 🛡️ 告警降噪建议\n检测到该告警在过去 1 小时内已发生 {recent_same_alerts} 次，可能存在抖动。建议检查相关阈值配置或开启静默。"
            analysis_result += noise_reduction_msg

        # 3. 匹配自愈策略
        matched_policy = None
        policies = SelfHealingPolicy.objects.filter(is_active=True)
        for policy in policies:
            match = True
            for key, value in policy.alert_match_rule.items():
                if alert.labels.get(key) != value:
                    match = False
                    break
            if match:
                matched_policy = policy
                break

        # 4. 更新告警状态
        alert.ai_analysis = analysis_result
        if matched_policy:
            alert.suggested_pipeline = matched_policy.pipeline
            # 固化策略信息：记录策略名称，记录当时是否为自动
            alert.matched_policy_name = matched_policy.name
            alert.trigger_type = 'auto' if matched_policy.is_auto_execute else 'manual'
        elif "__PIPELINE_DRAFT__:" in analysis_result:
            try:
                import json
                import time
                from apps.pipeline_management.models import Pipeline
                draft_str = analysis_result.split("__PIPELINE_DRAFT__:")[1].strip()
                start_idx = draft_str.find('{')
                end_idx = draft_str.rfind('}')
                if start_idx != -1 and end_idx != -1:
                    json_str = draft_str[start_idx:end_idx+1]
                    graph_data = json.loads(json_str)
                    
                    # 自动完善和编排流程图节点和依赖关系
                    if isinstance(graph_data, dict):
                        nodes = graph_data.get('nodes', [])
                        edges = graph_data.get('edges', [])
                        
                        from apps.pipeline_management.utils import normalize_and_filter_ai_dag
                        graph_data = normalize_and_filter_ai_dag(graph_data)
                        nodes = graph_data.get('nodes', [])
                        edges = graph_data.get('edges', [])

                        # 2. 补全底层执行实体及 fallback 依赖
                        from apps.task_management.models import AnsibleTask
                        from apps.host_management.models import ResourcePool
                        from apps.pipeline_management.models import CIEnvironment
                        from apps.registry_management.models import ImageRegistry
                        from apps.k8s_management.models import K8sCluster

                        for node in nodes:
                            node_type = node.get('type')
                            node_data = node.get('data', {})
                                
                            if node_type == 'ansible':
                                ansible_task_id = node_data.get('ansible_task_id')
                                task_exists = False
                                if ansible_task_id:
                                    try:
                                        task_exists = AnsibleTask.objects.filter(id=int(ansible_task_id)).exists()
                                    except (ValueError, TypeError):
                                        pass
                                        
                                if not task_exists:
                                    # 提取 AI 输入的指令或剧本
                                    playbook_content = (
                                        node_data.get('playbook') or 
                                        node_data.get('ansible_playbook') or
                                        node_data.get('content') or 
                                        node_data.get('playbook_content') or 
                                        node_data.get('cmd') or 
                                        node_data.get('exec') or
                                        node_data.get('command') or
                                        node_data.get('script') or
                                        node.get('playbook') or
                                        node.get('ansible_playbook') or
                                        node.get('content') or
                                        node.get('playbook_content') or
                                        node.get('cmd') or
                                        node.get('exec') or
                                        node.get('command') or
                                        node.get('script')
                                    )
                                    
                                    # 智能文件名-to-playbook转换逻辑
                                    if playbook_content and isinstance(playbook_content, str):
                                        content_str = playbook_content.strip()
                                        if '\n' not in content_str and (content_str.endswith('.yml') or content_str.endswith('.yaml')):
                                            import re
                                            filename = content_str
                                            check_match = re.match(r'check_port_(\d+)\.ya?ml', filename, re.IGNORECASE)
                                            fix_match = re.match(r'fix_port_(\d+)\.ya?ml', filename, re.IGNORECASE)
                                            
                                            if check_match:
                                                port = check_match.group(1)
                                                playbook_content = (
                                                    f"- name: Check Port {port} Status\n"
                                                    "  hosts: all\n"
                                                    "  gather_facts: false\n"
                                                    "  tasks:\n"
                                                    f"    - name: Wait for port {port} to be open\n"
                                                    "      wait_for:\n"
                                                    f"        port: {port}\n"
                                                    "        state: started\n"
                                                    "        timeout: 5\n"
                                                )
                                            elif fix_match:
                                                port = fix_match.group(1)
                                                playbook_content = (
                                                    f"- name: Fix Port {port} Conflict\n"
                                                    "  hosts: all\n"
                                                    "  gather_facts: false\n"
                                                    "  tasks:\n"
                                                    f"    - name: Kill processes using port {port}\n"
                                                    f"      shell: lsof -t -i:{port} | xargs -r kill -9\n"
                                                    "      failed_when: false\n"
                                                )
                                            elif 'disk' in filename.lower():
                                                playbook_content = (
                                                    "- name: Check Disk Usage\n"
                                                    "  hosts: all\n"
                                                    "  gather_facts: false\n"
                                                    "  tasks:\n"
                                                    "    - name: Check disk partitions\n"
                                                    "      shell: df -h\n"
                                                )
                                            elif 'cpu' in filename.lower() or 'load' in filename.lower():
                                                playbook_content = (
                                                    "- name: Check CPU Load\n"
                                                    "  hosts: all\n"
                                                    "  gather_facts: false\n"
                                                    "  tasks:\n"
                                                    "    - name: Get top cpu usage\n"
                                                    "      shell: ps -eo pcpu,pmem,args --sort=-pcpu | head -n 10\n"
                                                )
                                            elif 'mem' in filename.lower():
                                                playbook_content = (
                                                    "- name: Check Memory Usage\n"
                                                    "  hosts: all\n"
                                                    "  gather_facts: false\n"
                                                    "  tasks:\n"
                                                    "    - name: Free memory stats\n"
                                                    "      shell: free -m\n"
                                                )
                                            elif 'service' in filename.lower() or 'restart' in filename.lower():
                                                playbook_content = (
                                                    "- name: Restart Service\n"
                                                    "  hosts: all\n"
                                                    "  gather_facts: false\n"
                                                    "  tasks:\n"
                                                    "    - name: Try restart dummy service\n"
                                                    "      shell: echo 'Restarting dummy service'\n"
                                                )
                                            else:
                                                playbook_content = (
                                                    f"- name: AI Auto playbook fallback for {filename}\n"
                                                    "  hosts: all\n"
                                                    "  gather_facts: false\n"
                                                    "  tasks:\n"
                                                    f"    - name: Check status of {filename}\n"
                                                    f"      shell: echo 'Running diag for {filename}'\n"
                                                )
                                                
                                    if not playbook_content:
                                        # 兜底默认运维指令
                                        playbook_content = (
                                            "- name: Fallback Diagnostic Task\n"
                                            "  hosts: all\n"
                                            "  gather_facts: false\n"
                                            "  tasks:\n"
                                            "    - name: Check system uptime\n"
                                            "      shell: uptime\n"
                                            "    - name: Check disk usage\n"
                                            "      shell: df -h\n"
                                        )
                                        
                                    is_playbook = False
                                    if isinstance(playbook_content, str):
                                        pc_stripped = playbook_content.strip()
                                        if pc_stripped.startswith('-') or 'hosts:' in pc_stripped or 'tasks:' in pc_stripped:
                                            is_playbook = True
                                    task_type = 'playbook' if is_playbook else 'cmd'
                                    
                                    # 自动寻找资源池
                                    pool_id = node_data.get('resource_pool_id') or node_data.get('pool_id')
                                    resource_pool = None
                                    if pool_id:
                                        try:
                                            resource_pool = ResourcePool.objects.filter(id=int(pool_id)).first()
                                        except (ValueError, TypeError):
                                            pass
                                    if not resource_pool:
                                        resource_pool = ResourcePool.objects.first()
                                        
                                    # 规范化 hosts 占位符 (例如 hosts: '{{ instance }}') 为 hosts: all
                                    if playbook_content and isinstance(playbook_content, str):
                                        import re
                                        playbook_content = re.sub(
                                            r'hosts:\s*[\'"]?\{\{\s*(?:instance|host|target|target_host)\s*\}\}[\'"]?',
                                            'hosts: all',
                                            playbook_content
                                        )

                                    # 智能复用：计算待新建剧本的哈希，在 DB 中匹配是否存在相同剧本
                                    import hashlib
                                    p_content = playbook_content.strip() if playbook_content else ""
                                    p_hash = hashlib.sha256(p_content.encode('utf-8')).hexdigest() if p_content else ""
                                    
                                    existing_task = None
                                    if p_hash:
                                        existing_task = AnsibleTask.objects.filter(content_hash=p_hash).first()
                                        
                                    if existing_task:
                                        node_data['ansible_task_id'] = existing_task.id
                                        logger.info(f"[SRE] Reused existing AnsibleTask {existing_task.id} (hash match) for alert {alert.id}")
                                    else:
                                        task = AnsibleTask.objects.create(
                                            name=f"AI_Auto_Task_{alert.id}_{node.get('id')}",
                                            task_type=task_type,
                                            resource_pool=resource_pool,
                                            content=playbook_content,
                                            creator=get_system_bot(),
                                            create_type='ai'
                                        )
                                        node_data['ansible_task_id'] = task.id
                                        logger.info(f"[SRE] Auto-created AnsibleTask {task.id} (create_type='ai') for alert {alert.id}")
                                    
                            elif node_type == 'docker_build':
                                ci_env_id = node_data.get('ci_env_id')
                                env_exists = False
                                if ci_env_id:
                                    try:
                                        env_exists = CIEnvironment.objects.filter(id=int(ci_env_id)).exists()
                                    except (ValueError, TypeError):
                                        pass
                                if not env_exists:
                                    env_obj = CIEnvironment.objects.first()
                                    if not env_obj:
                                        env_obj = CIEnvironment.objects.create(
                                            name="Default Build Sandbox",
                                            image="alpine:latest",
                                            type="default",
                                            description="AI Auto-generated default build sandbox"
                                        )
                                    node_data['ci_env_id'] = env_obj.id
                                    logger.info(f"[SRE] Auto-bound CI environment {env_obj.id} to docker_build node")
                                    
                            elif node_type == 'kaniko_build':
                                registry_id = node_data.get('registry_id')
                                registry_exists = False
                                if registry_id:
                                    try:
                                        registry_exists = ImageRegistry.objects.filter(id=int(registry_id)).exists()
                                    except (ValueError, TypeError):
                                        pass
                                if not registry_exists:
                                    registry_obj = ImageRegistry.objects.first()
                                    if registry_obj:
                                        node_data['registry_id'] = registry_obj.id
                                        logger.info(f"[SRE] Auto-bound ImageRegistry {registry_obj.id} to kaniko_build node")
                                    else:
                                        logger.warning(f"[SRE] No ImageRegistry found in DB for kaniko_build node")
                                        
                            elif node_type == 'k8s_deploy':
                                cluster_id = node_data.get('k8s_cluster_id')
                                cluster_exists = False
                                if cluster_id:
                                    try:
                                        cluster_exists = K8sCluster.objects.filter(id=int(cluster_id)).exists()
                                    except (ValueError, TypeError):
                                        pass
                                if not cluster_exists:
                                    cluster_obj = K8sCluster.objects.first()
                                    if cluster_obj:
                                        node_data['k8s_cluster_id'] = cluster_obj.id
                                        logger.info(f"[SRE] Auto-bound K8sCluster {cluster_obj.id} to k8s_deploy node")
                                    else:
                                        logger.warning(f"[SRE] No K8sCluster found in DB for k8s_deploy node")
                                        
                            elif node_type == 'host_deploy':
                                pool_id = node_data.get('resource_pool_id')
                                pool_exists = False
                                if pool_id:
                                    try:
                                        pool_exists = ResourcePool.objects.filter(id=int(pool_id)).exists()
                                    except (ValueError, TypeError):
                                        pass
                                if not pool_exists:
                                    pool_obj = ResourcePool.objects.first()
                                    if pool_obj:
                                        node_data['resource_pool_id'] = pool_obj.id
                                        logger.info(f"[SRE] Auto-bound ResourcePool {pool_obj.id} to host_deploy node")
                                    else:
                                        logger.warning(f"[SRE] No ResourcePool found in DB for host_deploy node")
                    
                    dynamic_name = f"AI_Auto_Draft_{alert.id}_{int(time.time())}"
                    pipeline = Pipeline.objects.create(
                        name=dynamic_name,
                        desc=f"由 AI 为告警 {alert.alert_name} 自动生成的诊断修复流水线",
                        graph_data=graph_data,
                        creator=get_system_bot(),
                        is_active=True,
                        create_type='ai'
                    )
                    alert.suggested_pipeline = pipeline
                    alert.matched_policy_name = "AI 动态策略 (需确认)"
                    alert.trigger_type = 'manual'
                    logger.info(f"[SRE] Dynamic AI Pipeline created: {pipeline.id}")
            except Exception as e:
                logger.error(f"[SRE] Failed to parse AI Pipeline Draft: {e}")
        
        alert.healing_status = 'suggested'
        alert.save()

        # 5. 如果是自动执行策略，直接触发
        if matched_policy and matched_policy.is_auto_execute and alert.status == 'firing':
            # 5.1 [熔断保护] 统计过去 1 小时内相同告警指纹且为自动触发的自愈记录
            recent_auto_runs = AlertEvent.objects.filter(
                fingerprint=alert.fingerprint,
                trigger_type='auto',
                create_time__gte=timezone.now() - timezone.timedelta(hours=1)
            ).count()

            if recent_auto_runs >= 3:
                # 触发熔断
                alert.healing_status = 'awaiting_approval'
                alert.trigger_type = 'manual'
                
                # 自动为该熔断创建审批工单
                from apps.approval_center.engine import ProxyApprovalEngine
                
                class MockRequestForBreaker:
                    def __init__(self, user, data, path):
                        self.user = user
                        self.data = data
                        self.path = path
                        self.method = 'POST'
                    def get_full_path(self):
                        return self.path

                bot = get_system_bot()
                mock_request = MockRequestForBreaker(
                    user=bot,
                    data={
                        "alert_id": alert.id,
                        "pipeline_id": alert.suggested_pipeline.id,
                        "alert_name": alert.alert_name,
                        "reason": "AI Self-healing Breaker Tripped - Manual Confirmation Required",
                        "ai_verified": False
                    },
                    path=f"/api/v1/pipelines/{alert.suggested_pipeline.id}/execute/"
                )
                
                # 强制阻断并提交审批工单
                environment = alert.labels.get('env') or alert.labels.get('environment')
                try:
                    is_blocked, approval_res = ProxyApprovalEngine.intercept_if_needed(
                        mock_request,
                        resource_type='pipeline:run',
                        action_title=f"自愈熔断审批: {alert.alert_name} -> 触发流水线 #{alert.suggested_pipeline.id}",
                        target_id=str(alert.suggested_pipeline.id),
                        environment=environment,
                        extra_context={"alert_id": alert.id, "trigger_source": "auto_self_healing_breaker"}
                    )
                    
                    if is_blocked:
                        ticket_id = approval_res.data.get('ticket_id')
                        alert.latest_ticket_id = ticket_id
                except Exception as e_app:
                    logger.error(f"[SRE] Failed to intercept for circuit breaker: {e_app}")
                    
                breaker_msg = (
                    f"\n\n### 🚨 [熔断保护已触发] 自愈执行限制\n"
                    f"检测到该告警指纹在过去 1 小时内已自动触发过 {recent_auto_runs} 次自愈。"
                    f"为防止自愈死循环或故障扩大，自愈决策已自动执行熔断。“自动触发”已强制降级为“人工审批确认”，本次自愈需要管理员手动审批。"
                )
                alert.ai_analysis = (alert.ai_analysis or "") + breaker_msg
                alert.save()
                logger.warning(f"[SRE] Circuit breaker tripped for alert {alert.id} (fingerprint: {alert.fingerprint}). Auto execution blocked.")
            else:
                trigger_self_healing.delay(alert.id)

    except Exception as e:
        logger.error(f"AI analysis failed for alert {alert_id}: {str(e)}")
        if alert:
            alert.ai_analysis = f"AI 分析失败: {str(e)}"
            alert.healing_status = 'failed'
            alert.save()
        raise e

    return f"Analysis complete for alert {alert_id}"

def get_system_bot():
    """获取或创建系统机器人账号（授予超管权限以通过内部权限校验）"""
    from apps.rbac_permission.models import User
    bot, created = User.objects.get_or_create(
        username='system_bot',
        defaults={
            'first_name': 'System',
            'last_name': 'Bot',
            'is_active': True,
            'is_superuser': True,
            'is_staff': True
        }
    )
    if not created and not bot.is_superuser:
        bot.is_superuser = True
        bot.is_staff = True
        bot.save(update_fields=['is_superuser', 'is_staff'])
    return bot

class MockRequest:
    """虚拟 HttpRequest 载体，用于触发审批引擎"""
    def __init__(self, user, data, path='/api/v1/sre/self-healing/trigger/', method='POST'):
        self.user = user
        self.data = data
        self.method = method
        self.META = {}
        self._full_path = path
        self._is_approved_execution = False # 显式定义，防止拦截器 getattr 失败（虽然 getattr 有默认值）

    def get_full_path(self):
        return self._full_path

@shared_task(name="apps.sre_management.tasks.trigger_self_healing")
def trigger_self_healing(alert_id):
    """
    执行自愈流水线（支持审批拦截）
    """
    from apps.pipeline_management.models import PipelineRun
    from apps.pipeline_management.tasks import advance_pipeline_engine
    from apps.approval_center.engine import ProxyApprovalEngine
    import traceback
    
    try:
        alert = AlertEvent.objects.get(id=alert_id)
        if not alert.suggested_pipeline:
            return "No pipeline suggested"

        # 1. 构造虚拟请求上下文
        bot = get_system_bot()
        payload = {
            "alert_id": alert.id,
            "pipeline_id": alert.suggested_pipeline.id,
            "alert_name": alert.alert_name,
            "reason": "AI Self-healing Auto Trigger",
            "ai_verified": True,  # [优化 D] 注入 AI 确信标志，配合白名单策略使用
            "_developer_ref": "creed"
        }
        
        # 确定环境（从标签中获取，默认为空）
        environment = alert.labels.get('env') or alert.labels.get('environment')
        
        # 模拟请求对象
        mock_request = MockRequest(
            user=bot,
            data=payload,
            path=f"/api/v1/pipelines/{alert.suggested_pipeline.id}/execute/"
        )

        # 2. 尝试触发拦截器
        try:
            is_blocked, approval_res = ProxyApprovalEngine.intercept_if_needed(
                mock_request,
                resource_type='pipeline:run',
                action_title=f"自愈审批: {alert.alert_name} -> 触发流水线 #{alert.suggested_pipeline.id}",
                target_id=str(alert.suggested_pipeline.id),
                environment=environment,
                extra_context={"alert_id": alert.id, "trigger_source": "auto_self_healing"}
            )
        except Exception as intercept_err:
            logger.error(f"Critical error in ProxyApprovalEngine: {str(intercept_err)}\n{traceback.format_exc()}")
            # 如果拦截器本身崩了，为了安全起见，我们暂不自动触发，而是标记为失败
            alert.healing_status = 'failed'
            alert.ai_analysis = (alert.ai_analysis or "") + f"\n\n[拦截器异常] {str(intercept_err)}"
            alert.save()
            return f"Error in interception engine: {str(intercept_err)}"

        if is_blocked:
            # 被拦截：更新告警状态为“待审批”，并记录工单 ID
            ticket_id = approval_res.data.get('ticket_id')
            alert.healing_status = 'awaiting_approval'
            alert.latest_ticket_id = ticket_id
            alert.save()
            logger.info(f"Self-healing for alert {alert_id} is blocked by approval policy. Ticket ID: {ticket_id}")
            return f"Blocked by approval policy. Ticket ID: {ticket_id}. Environment: {environment}"

        # 3. 未被拦截：直接执行（原有逻辑）
        alert.healing_status = 'executing'
        alert.save()

        # [优化] 将告警上下文注入流水线变量池
        pipeline_vars = {
            "alert": {
                "id": alert.id,
                "name": alert.alert_name,
                "labels": alert.labels,
                "severity": alert.severity
            }
        }

        # 创建流水线运行记录
        run = PipelineRun.objects.create(
            pipeline=alert.suggested_pipeline,
            status='pending',
            trigger_user=bot,
            trigger_type='automation',
            extra_vars=pipeline_vars # 注入变量
        )

        # 记录关键信息到告警事件
        alert.latest_run_id = run.id
        alert.trigger_type = 'auto'
        alert.healing_status = 'executing'
        alert.save(update_fields=['latest_run_id', 'trigger_type', 'healing_status'])

        # 触发流水线执行引擎
        advance_pipeline_engine.delay(run.id)
        
        logger.info(f"Self-healing triggered for alert {alert_id} using pipeline {alert.suggested_pipeline.id}, run_id: {run.id}")
        
    except Exception as e:
        logger.error(f"Failed to trigger self-healing for alert {alert_id}: {str(e)}")
        try:
            alert = AlertEvent.objects.get(id=alert_id)
            alert.healing_status = 'failed'
            alert.save()
        except:
            pass
        raise e


@shared_task(name="apps.sre_management.tasks.export_alert_report_task")
def export_alert_report_task(user_id, start_time_str, end_time_str):
    import datetime
    import csv
    import uuid
    import os
    from django.conf import settings
    from django.utils import timezone
    from django.utils.dateparse import parse_datetime, parse_date
    from django.db.models import Count, Q
    from apps.sre_management.models import AlertEvent
    from apps.system_management.models import UserNotification
    from asgiref.sync import async_to_sync
    from channels.layers import get_channel_layer

    def parse_date_param(param_str, is_end=False):
        if not param_str:
            return None
        try:
            dt = parse_datetime(param_str)
            if dt:
                if timezone.is_naive(dt):
                    dt = timezone.make_aware(dt)
                return dt
        except Exception:
            pass
        try:
            d = parse_date(param_str)
            if d:
                dt = datetime.datetime.combine(d, datetime.time.max if is_end else datetime.time.min)
                return timezone.make_aware(dt)
        except Exception:
            pass
        return None

    start_time = parse_date_param(start_time_str, is_end=False)
    end_time = parse_date_param(end_time_str, is_end=True)

    if not start_time:
        start_time = timezone.now() - datetime.timedelta(days=7)
    if not end_time:
        end_time = timezone.now()

    events = AlertEvent.objects.filter(create_time__range=(start_time, end_time))

    name_stats = events.values('alert_name', 'severity') \
                       .annotate(
                           count=Count('id'),
                           resolved_count=Count('id', filter=Q(status='resolved')),
                           healing_count=Count('id', filter=Q(healing_status__in=['executing', 'success', 'failed'])),
                           healing_success_count=Count('id', filter=Q(healing_status='success')),
                           healing_failed_count=Count('id', filter=Q(healing_status='failed'))
                       ) \
                       .order_by('-count')

    # Ensure reports directory exists inside media
    reports_dir = os.path.join(settings.MEDIA_ROOT, 'reports')
    os.makedirs(reports_dir, exist_ok=True)

    filename = f"sre_alert_report_{uuid.uuid4().hex}.csv"
    filepath = os.path.join(reports_dir, filename)
    file_url = f"{settings.MEDIA_URL}reports/{filename}"

    with open(filepath, 'w', encoding='utf-8-sig', newline='') as f:
        writer = csv.writer(f)
        writer.writerow([
            '告警名称', '严重程度', '发生次数', '已恢复次数', '恢复率 (%)',
            '自愈执行次数', '自愈成功次数', '自愈失败次数', '自愈成功率 (%)'
        ])

        for item in name_stats:
            total = item['count']
            resolved = item['resolved_count']
            healing = item['healing_count']
            success = item['healing_success_count']
            failed = item['healing_failed_count']

            recovery_rate = round(resolved * 100.0 / total, 2) if total > 0 else 0.0
            healing_success_rate = round(success * 100.0 / (success + failed), 2) if (success + failed) > 0 else 0.0

            writer.writerow([
                item['alert_name'],
                item['severity'],
                total,
                resolved,
                f"{recovery_rate}%",
                healing,
                success,
                failed,
                f"{healing_success_rate}%"
            ])

    # Save UserNotification
    notification = UserNotification.objects.create(
        user_id=user_id,
        title="告警自愈报表生成成功",
        content=f"您于 {timezone.now().strftime('%Y-%m-%d %H:%M:%S')} 导出的告警报表已生成完毕，点击直接下载。",
        extra_data={"download_url": file_url, "type": "report_ready"}
    )

    # Broadcast via websocket group
    channel_layer = get_channel_layer()
    if channel_layer:
        try:
            async_to_sync(channel_layer.group_send)(
                f"user_notifications_{user_id}",
                {
                    "type": "send_notification",
                    "data": {
                        "id": notification.id,
                        "title": notification.title,
                        "content": notification.content,
                        "is_read": notification.is_read,
                        "create_time": notification.create_time.isoformat(),
                        "extra_data": notification.extra_data
                    }
                }
            )
        except Exception as exc:
            logger.warning("[SRE] Failed to broadcast alert report notification: %s", exc)


@shared_task(name="apps.sre_management.tasks.run_timepoint_diagnosis", bind=True, max_retries=2)
def run_timepoint_diagnosis(self, diagnosis_id):
    """异步执行时间点诊断。"""
    import json
    from apps.approval_center.models import ApprovalTicket
    from apps.pipeline_management.models import PipelineRun
    from apps.task_management.models import AnsibleExecution
    from .diagnosis_utils import build_evidence_index, extract_log_highlights, extract_structured_report
    from .observability import get_log_adapter, get_metric_adapter

    run = DiagnosisRun.objects.select_related('service', 'project', 'alert', 'template').filter(id=diagnosis_id).first()
    if not run:
        logger.warning("[SRE Diagnosis] DiagnosisRun %s not found", diagnosis_id)
        return

    run.status = 'running'
    run.started_at = timezone.now()
    run.error_message = None
    run.save(update_fields=['status', 'started_at', 'error_message'])

    try:
        service = run.service
        template_snapshot = _template_snapshot_for_run(run)
        template_collection = _template_collection_config(template_snapshot)
        start = run.diagnosis_time - timezone.timedelta(minutes=run.window_minutes)
        end = run.diagnosis_time + timezone.timedelta(minutes=run.window_minutes)
        context = {
            'diagnosis': {
                'id': run.id,
                'title': run.title,
                'time': run.diagnosis_time.isoformat(),
                'window_minutes': run.window_minutes,
                'trigger_type': run.trigger_type,
            },
            'project': {
                'id': run.project_id,
                'name': getattr(run.project, 'name', None),
                'code': getattr(run.project, 'code', None),
            },
            'template': {
                'id': (template_snapshot or {}).get('id'),
                'code': (template_snapshot or {}).get('code'),
                'name': (template_snapshot or {}).get('name'),
                'category': (template_snapshot or {}).get('category'),
                'target_type': ((template_snapshot or {}).get('content') or {}).get('target_type'),
            } if template_snapshot else None,
            'service': None,
            'metrics': [],
            'metric_contexts': [],
            'logs': None,
            'log_contexts': [],
            'log_highlights': [],
            'service_match': (run.query_params or {}).get('service_match'),
            'warnings': [],
            'collection_summary': {
                'metrics': {'status': 'skipped', 'datasource': None, 'datasources': [], 'count': 0},
                'logs': {'status': 'skipped', 'datasource': None, 'datasources': [], 'count': 0},
                'log_highlights': {'status': 'skipped', 'count': 0},
                'ansflow_events': {'status': 'pending', 'count': 0},
                'ci_cd_context': {'status': 'skipped', 'count': 0},
            },
            'ansflow_events': {},
            'ci_cd_context': {},
        }
        warnings = context['warnings']

        if service:
            metric_datasources = _select_metric_datasources(service, template_snapshot)
            log_datasources = _select_log_datasources(service, template_snapshot)
            context['service'] = {
                'id': service.id,
                'name': service.name,
                'code': service.code,
                'namespace': service.namespace,
                'metric_label_selector': service.metric_label_selector,
                'log_label_selector': service.log_label_selector,
            }
            collect_metrics = template_collection.get('metrics', True)
            collect_service_logs = template_collection.get('service_logs', True)
            if metric_datasources and collect_metrics:
                context['collection_summary']['metrics']['datasources'] = [
                    {'id': ds.id, 'name': ds.name, 'provider': ds.provider}
                    for ds in metric_datasources
                ]
                context['collection_summary']['metrics']['datasource'] = context['collection_summary']['metrics']['datasources'][0]
                total_metric_count = 0
                successful_sources = 0
                failed_sources = 0
                try:
                    for metric_ds in metric_datasources:
                        try:
                            metrics = get_metric_adapter(metric_ds).query_metrics(service, start, end)
                            metric_context = _normalize_metric_context(metric_ds, metrics, start, end)
                            context['metric_contexts'].append(metric_context)
                            total_metric_count += metric_context['count']
                            successful_sources += 1
                            if not context['metrics']:
                                context['metrics'] = metric_context['metrics']
                        except Exception as metric_exc:
                            failed_sources += 1
                            warning = f"指标数据源 {metric_ds.name} 采集失败：{metric_exc}"
                            warnings.append(warning)
                            logger.warning("[SRE Diagnosis] %s", warning)

                    if successful_sources and failed_sources:
                        context['collection_summary']['metrics']['status'] = 'partial'
                    elif successful_sources:
                        context['collection_summary']['metrics']['status'] = 'success'
                    else:
                        context['collection_summary']['metrics']['status'] = 'failed'
                    context['collection_summary']['metrics']['count'] = total_metric_count
                    context['collection_summary']['metrics']['source_count'] = successful_sources
                    context['collection_summary']['metrics']['failed_source_count'] = failed_sources
                except Exception as metric_exc:
                    warning = f"服务指标采集失败：{metric_exc}"
                    warnings.append(warning)
                    context['collection_summary']['metrics']['status'] = 'failed'
                    context['collection_summary']['metrics']['error'] = str(metric_exc)
                    logger.warning("[SRE Diagnosis] %s", warning)
            elif not collect_metrics:
                context['collection_summary']['metrics']['status'] = 'skipped'
                warnings.append("当前诊断模板未启用服务指标采集。")
            else:
                warnings.append("未配置指标数据源，本次诊断将跳过指标上下文。")

            if log_datasources and collect_service_logs:
                context['collection_summary']['logs']['datasources'] = [
                    {'id': ds.id, 'name': ds.name, 'provider': ds.provider}
                    for ds in log_datasources
                ]
                context['collection_summary']['logs']['datasource'] = context['collection_summary']['logs']['datasources'][0]
                total_log_count = 0
                total_highlight_count = 0
                successful_sources = 0
                failed_sources = 0
                try:
                    for log_ds in log_datasources:
                        try:
                            logs = get_log_adapter(log_ds).query_logs(service, start, end)
                            highlights = extract_log_highlights(logs)
                            log_context = _normalize_log_context(log_ds, logs, highlights, start, end)
                            context['log_contexts'].append(log_context)
                            total_log_count += log_context['count']
                            total_highlight_count += log_context['highlight_count']
                            successful_sources += 1
                            if context['logs'] is None:
                                context['logs'] = logs
                                context['log_highlights'] = log_context['highlights']
                        except Exception as log_exc:
                            failed_sources += 1
                            warning = f"日志数据源 {log_ds.name} 采集失败：{log_exc}"
                            warnings.append(warning)
                            logger.warning("[SRE Diagnosis] %s", warning)

                    if successful_sources and failed_sources:
                        context['collection_summary']['logs']['status'] = 'partial'
                    elif successful_sources:
                        context['collection_summary']['logs']['status'] = 'success'
                    else:
                        context['collection_summary']['logs']['status'] = 'failed'
                    context['collection_summary']['logs']['count'] = total_log_count
                    context['collection_summary']['logs']['source_count'] = successful_sources
                    context['collection_summary']['logs']['failed_source_count'] = failed_sources
                    context['collection_summary']['log_highlights'] = {
                        'status': 'success' if total_highlight_count else ('skipped' if successful_sources else 'failed'),
                        'count': total_highlight_count,
                    }
                except Exception as log_exc:
                    warning = f"服务日志采集失败：{log_exc}"
                    warnings.append(warning)
                    context['collection_summary']['logs']['status'] = 'failed'
                    context['collection_summary']['logs']['error'] = str(log_exc)
                    context['collection_summary']['log_highlights']['status'] = 'failed'
                    logger.warning("[SRE Diagnosis] %s", warning)
            elif not collect_service_logs:
                context['collection_summary']['logs']['status'] = 'skipped'
                context['collection_summary']['log_highlights']['status'] = 'skipped'
                warnings.append("当前诊断模板未启用服务日志采集。")
            else:
                warnings.append("未配置日志数据源，本次诊断将跳过日志上下文。")
        else:
            warnings.append("未选择可观测服务，本次诊断仅使用 AnsFlow 内部上下文。")

        pipeline_filter = {'pipeline__project_id': run.project_id} if run.project_id else {}
        ansible_filter = {'task__project_id': run.project_id} if run.project_id else {}
        context['ansflow_events'] = {
            'alerts': list(AlertEvent.objects.filter(create_time__range=(start, end)).values(
                'id', 'alert_name', 'severity', 'status', 'source', 'labels', 'annotations', 'healing_status', 'create_time'
            )[:20]),
            'pipeline_runs': list(PipelineRun.objects.filter(create_time__range=(start, end), **pipeline_filter).values(
                'id', 'pipeline_id', 'status', 'trigger_type', 'create_time', 'update_time'
            )[:20]),
            'ansible_executions': list(AnsibleExecution.objects.filter(create_time__range=(start, end), **ansible_filter).values(
                'id', 'task_id', 'status', 'create_time', 'update_time'
            )[:20]),
            'approval_tickets': list(ApprovalTicket.objects.filter(create_time__range=(start, end)).values(
                'id', 'title', 'status', 'resource_type', 'create_time', 'audit_time'
            )[:20]),
        }
        context['collection_summary']['ansflow_events'] = {
            'status': 'success',
            'count': sum(len(value) for value in context['ansflow_events'].values()),
        }
        if template_snapshot:
            context['ci_cd_context'] = _collect_ci_cd_context(run, start, end, template_snapshot)
            context['collection_summary']['ci_cd_context'] = {
                'status': 'success',
                'count': sum(
                    summary.get('count', 0)
                    for summary in (context['ci_cd_context'].get('collection_summary') or {}).values()
                    if isinstance(summary, dict)
                ),
            }
        if run.alert:
            context['source_alert'] = {
                'id': run.alert_id,
                'alert_name': run.alert.alert_name,
                'severity': run.alert.severity,
                'labels': run.alert.labels,
                'annotations': run.alert.annotations,
            }
        context['evidence_index'] = build_evidence_index(context)
        context['structured_report'] = {}

        rag_service = RAGService()
        prompt_context = json.dumps(context, ensure_ascii=False, default=str)[:24000]
        prompt_vars = {
            'prefix': rag_service.personality.get('prefix', ''),
            'diagnosis_context': prompt_context,
        }
        prompt_template = ((template_snapshot or {}).get('content') or {}).get('prompt_template') or rag_service._get_prompt("timepoint_diagnosis")
        try:
            prompt = prompt_template.format(**prompt_vars)
        except Exception as prompt_exc:
            from apps.ai_engine.prompt_defaults import DEFAULT_PROMPTS
            logger.warning("[SRE Diagnosis] Failed to format diagnosis prompt: %s", prompt_exc)
            context['warnings'].append(f"诊断模板 Prompt 格式化失败，已使用默认模板：{prompt_exc}")
            prompt = DEFAULT_PROMPTS["timepoint_diagnosis"]["template"].format(**prompt_vars)
        chain = rag_service.get_chat_chain()
        raw_ai_result = chain.invoke(prompt)
        structured_report, ai_result, parse_warning = extract_structured_report(raw_ai_result)
        context['structured_report'] = structured_report
        if parse_warning:
            context['warnings'].append(parse_warning)

        run.context_snapshot = context
        run.ai_result = ai_result
        run.status = 'success'
        run.finished_at = timezone.now()
        run.save(update_fields=['context_snapshot', 'ai_result', 'status', 'finished_at'])
    except Exception as exc:
        logger.exception("[SRE Diagnosis] Failed to run diagnosis %s", diagnosis_id)
        run.status = 'failed'
        run.error_message = str(exc)
        run.finished_at = timezone.now()
        run.save(update_fields=['status', 'error_message', 'finished_at'])
        raise
