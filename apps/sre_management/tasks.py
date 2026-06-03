import logging
import os
from celery import shared_task
from .models import AlertEvent, DiagnosisRun, ObservabilityDataSource, SelfHealingPolicy
from apps.ai_engine.rag_service import RAGService
from django.utils import timezone

# Fix for macOS Celery fork safety
os.environ["OBJC_DISABLE_INITIALIZE_FORK_SAFETY"] = "YES"
# Disable parallel tokenizers to avoid SIGSEGV in forked processes
os.environ["TOKENIZERS_PARALLELISM"] = "false"

logger = logging.getLogger(__name__)

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

    run = DiagnosisRun.objects.select_related('service', 'project', 'alert').filter(id=diagnosis_id).first()
    if not run:
        logger.warning("[SRE Diagnosis] DiagnosisRun %s not found", diagnosis_id)
        return

    run.status = 'running'
    run.started_at = timezone.now()
    run.error_message = None
    run.save(update_fields=['status', 'started_at', 'error_message'])

    try:
        service = run.service
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
            'service': None,
            'metrics': [],
            'logs': None,
            'log_highlights': [],
            'service_match': (run.query_params or {}).get('service_match'),
            'warnings': [],
            'collection_summary': {
                'metrics': {'status': 'skipped', 'datasource': None, 'count': 0},
                'logs': {'status': 'skipped', 'datasource': None, 'count': 0},
                'log_highlights': {'status': 'skipped', 'count': 0},
                'ansflow_events': {'status': 'pending', 'count': 0},
            },
            'ansflow_events': {},
        }
        warnings = context['warnings']

        if service:
            metric_ds = service.metric_datasource or ObservabilityDataSource.objects.filter(kind='metric', is_default=True, is_active=True).first()
            log_ds = service.log_datasource or ObservabilityDataSource.objects.filter(kind='log', is_default=True, is_active=True).first()
            context['service'] = {
                'id': service.id,
                'name': service.name,
                'code': service.code,
                'namespace': service.namespace,
                'metric_label_selector': service.metric_label_selector,
                'log_label_selector': service.log_label_selector,
            }
            if metric_ds:
                context['collection_summary']['metrics']['datasource'] = {
                    'id': metric_ds.id,
                    'name': metric_ds.name,
                    'provider': metric_ds.provider,
                }
                try:
                    context['metrics'] = get_metric_adapter(metric_ds).query_metrics(service, start, end)
                    context['collection_summary']['metrics']['status'] = 'success'
                    context['collection_summary']['metrics']['count'] = len(context['metrics'])
                except Exception as metric_exc:
                    warning = f"指标数据源 {metric_ds.name} 采集失败：{metric_exc}"
                    warnings.append(warning)
                    context['collection_summary']['metrics']['status'] = 'failed'
                    context['collection_summary']['metrics']['error'] = str(metric_exc)
                    logger.warning("[SRE Diagnosis] %s", warning)
            else:
                warnings.append("未配置指标数据源，本次诊断将跳过指标上下文。")

            if log_ds:
                context['collection_summary']['logs']['datasource'] = {
                    'id': log_ds.id,
                    'name': log_ds.name,
                    'provider': log_ds.provider,
                }
                try:
                    context['logs'] = get_log_adapter(log_ds).query_logs(service, start, end)
                    log_items = context['logs'].get('items') if isinstance(context['logs'], dict) else []
                    context['collection_summary']['logs']['status'] = 'success'
                    context['collection_summary']['logs']['count'] = len(log_items or [])
                    context['log_highlights'] = extract_log_highlights(context['logs'])
                    context['collection_summary']['log_highlights'] = {
                        'status': 'success',
                        'count': len(context['log_highlights']),
                    }
                except Exception as log_exc:
                    warning = f"日志数据源 {log_ds.name} 采集失败：{log_exc}"
                    warnings.append(warning)
                    context['collection_summary']['logs']['status'] = 'failed'
                    context['collection_summary']['logs']['error'] = str(log_exc)
                    context['collection_summary']['log_highlights']['status'] = 'skipped'
                    logger.warning("[SRE Diagnosis] %s", warning)
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

        prompt_context = json.dumps(context, ensure_ascii=False, default=str)[:24000]
        prompt = (
            "你是资深 SRE。请基于以下时间点诊断上下文，分析系统或项目在该时间窗口的异常现象、"
            "可能根因、需要继续验证的证据、建议处置步骤。请优先关联日志、指标、告警、流水线和任务记录。"
            "如果某类上下文缺失，请明确说明本次诊断的证据限制。"
            "请先输出一段固定格式的结构化 JSON，格式为 __STRUCTURED_REPORT__:{...}。"
            "JSON 必须包含 summary、impact_scope、evidence、possible_causes、recommended_actions、risks、next_checks。"
            "其中 evidence 使用 {ref, finding}，possible_causes 使用 {title, confidence, evidence_refs}，"
            "recommended_actions 使用 {action, priority, evidence_refs}。"
            "所有 evidence_refs 尽量引用 evidence_index 中的 ref，例如 LOG-1、METRIC-1、ALERT-1。"
            "结构化 JSON 后面再输出 Markdown 诊断报告。\n\n"
            f"{prompt_context}"
        )

        rag_service = RAGService()
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
