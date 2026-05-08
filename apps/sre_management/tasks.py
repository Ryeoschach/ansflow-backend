import logging
from celery import shared_task
from .models import AlertEvent, SelfHealingPolicy
from apps.ai_engine.rag_service import RAGService
from django.utils import timezone

logger = logging.getLogger(__name__)

@shared_task(queue='ai_queue')
def analyze_alert_event(alert_id):
    """
    异步 AI 分析告警事件并匹配自愈策略
    """
    try:
        alert = AlertEvent.objects.get(id=alert_id)
    except AlertEvent.DoesNotExist:
        logger.error(f"Alert event {alert_id} not found")
        return

    alert.healing_status = 'analyzing'
    alert.save()

    # 1. 构造 RAG 查询问题
    labels_str = ", ".join([f"{k}={v}" for k, v in alert.labels.items()])
    question = f"我收到了一个名为 '{alert.alert_name}' 的告警，严重程度为 '{alert.severity}'。标签信息: {labels_str}。请根据历史经验给出诊断建议。"

    # 2. 调用 RAG 引擎
    rag_service = RAGService()
    analysis_result = ""
    try:
        # 这里我们使用同步方式获取全部结果（因为是后台任务）
        # RAGService.get_chat_chain().invoke() 会返回完整字符串
        chain = rag_service.get_chat_chain()
        analysis_result = chain.invoke(question)
    except Exception as e:
        logger.error(f"AI analysis failed for alert {alert_id}: {str(e)}")
        analysis_result = f"AI 分析失败: {str(e)}"

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

    return f"Analysis complete for alert {alert_id}"

@shared_task(queue='dag_queue')
def trigger_self_healing(alert_id):
    """
    执行自愈流水线
    """
    from apps.pipeline_management.tasks import execute_pipeline_task
    
    alert = AlertEvent.objects.get(id=alert_id)
    if not alert.suggested_pipeline:
        return "No pipeline suggested"

    alert.healing_status = 'executing'
    alert.save()

    # 触发流水线执行逻辑
    # 模拟手动触发：传入 pipeline_id, trigger_user=None, trigger_type='automation'
    # 注意：这里需要根据 execute_pipeline_task 的具体签名来调用
    # 假设 execute_pipeline_task(pipeline_id, trigger_user_id=None)
    try:
        # 这里只是示例，实际需要对接 pipeline_management 的具体执行入口
        # execute_pipeline_task.delay(alert.suggested_pipeline.id)
        logger.info(f"Self-healing triggered for alert {alert_id} using pipeline {alert.suggested_pipeline.id}")
        # 执行成功后更新状态（实际应监听流水线完成的回调）
        # alert.healing_status = 'success'
        # alert.save()
    except Exception as e:
        alert.healing_status = 'failed'
        alert.save()
        logger.error(f"Failed to trigger self-healing for alert {alert_id}: {str(e)}")
