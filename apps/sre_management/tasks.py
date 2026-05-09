import logging
import os
from celery import shared_task
from .models import AlertEvent, SelfHealingPolicy
from apps.ai_engine.rag_service import RAGService
from django.utils import timezone

# Fix for macOS Celery fork safety
os.environ["OBJC_DISABLE_INITIALIZE_FORK_SAFETY"] = "YES"
# Disable parallel tokenizers to avoid SIGSEGV in forked processes
os.environ["TOKENIZERS_PARALLELISM"] = "false"

logger = logging.getLogger(__name__)

@shared_task(bind=True, max_retries=3)
def analyze_alert_event(self, alert_id):
    """
    异步 AI 分析告警事件并匹配自愈策略
    """
    alert = None
    try:
        alert = AlertEvent.objects.get(id=alert_id)
        alert.healing_status = 'analyzing'
        alert.save()

        # 1. 构造 RAG 查询问题
        labels_str = ", ".join([f"{k}={v}" for k, v in alert.labels.items()])
        question = f"我收到了一个名为 '{alert.alert_name}' 的告警，严重程度为 '{alert.severity}'。标签信息: {labels_str}。请根据历史经验给出诊断建议。"

        # 2. 调用 RAG 引擎
        rag_service = RAGService()
        analysis_result = ""
        
        # 使用同步方式获取全部结果
        chain = rag_service.get_chat_chain()
        analysis_result = chain.invoke(question)

        # 3. 匹配自愈策略
        suggested_pipeline = None
        policies = SelfHealingPolicy.objects.filter(is_active=True)
        for policy in policies:
            match = True
            for key, value in policy.alert_match_rule.items():
                if alert.labels.get(key) != value:
                    match = False
                    break
            if match:
                suggested_pipeline = policy.pipeline
                break

        # 4. 更新告警状态
        alert.ai_analysis = analysis_result
        alert.suggested_pipeline = suggested_pipeline
        alert.healing_status = 'suggested'
        alert.save()

        # 5. 如果是自动执行策略，直接触发
        if suggested_pipeline and alert.status == 'firing':
            policy = SelfHealingPolicy.objects.filter(pipeline=suggested_pipeline, is_active=True).first()
            if policy and policy.is_auto_execute:
                trigger_self_healing.delay(alert.id)

    except Exception as e:
        logger.error(f"AI analysis failed for alert {alert_id}: {str(e)}")
        if alert:
            alert.ai_analysis = f"AI 分析失败: {str(e)}"
            alert.healing_status = 'failed'
            alert.save()
        raise e

    return f"Analysis complete for alert {alert_id}"

@shared_task
def trigger_self_healing(alert_id):
    """
    执行自愈流水线
    """
    from apps.pipeline_management.models import PipelineRun
    from apps.pipeline_management.tasks import advance_pipeline_engine
    
    try:
        alert = AlertEvent.objects.get(id=alert_id)
        if not alert.suggested_pipeline:
            return "No pipeline suggested"

        alert.healing_status = 'executing'
        alert.save()

        # 创建流水线运行记录
        run = PipelineRun.objects.create(
            pipeline=alert.suggested_pipeline,
            status='pending',
            trigger_user=None, # 系统自动触发
            trigger_type='automation'
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
