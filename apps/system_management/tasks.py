import logging
from celery import shared_task
from django.utils import timezone
from django.core.cache import cache
from config.celery import app as celery_app
from django_redis import get_redis_connection

logger = logging.getLogger(__name__)

@shared_task(name="apps.system_management.tasks.collect_celery_stats")
def collect_celery_stats():
    """
    后台异步采集 Celery 统计信息并更新缓存。
    解耦 API 响应，解决同步广播导致的延迟。
    """
    cache_key = "ansflow:system:celery_stats"
    
    try:
        # 允许稍长的超时，因为是后台任务
        inspector = celery_app.control.inspect(timeout=3.0)

        # 1. 基础任务统计 (批量发起)
        active = inspector.active() or {}
        scheduled = inspector.scheduled() or {}
        reserved = inspector.reserved() or {}
        stats = inspector.stats() or {}
        
        # 2. 汇总 Worker 信息
        worker_details = []
        all_workers = set(list(active.keys()) + list(scheduled.keys()) + list(reserved.keys()) + list(stats.keys()))
        
        for worker in all_workers:
            w_stats = stats.get(worker, {})
            worker_details.append({
                "worker": worker,
                "status": "online" if worker in active or worker in stats else "offline",
                "active_count": len(active.get(worker, [])),
                "scheduled_count": len(scheduled.get(worker, [])),
                "reserved_count": len(reserved.get(worker, [])),
                "concurrency": w_stats.get('pool', {}).get('max-concurrency'),
                "broker_transport": w_stats.get('broker', {}).get('transport'),
                "rusage": w_stats.get('rusage', {})
            })
        
        # 3. 队列积压情况 (从 Redis 获取)
        queue_stats = []
        try:
            conn = get_redis_connection("default")
            for q_name in ['celery']:
                queue_stats.append({
                    "name": q_name,
                    "length": conn.llen(q_name)
                })
        except Exception: pass
        
        # 4. Beat 状态
        from django_celery_beat.models import PeriodicTask
        recent_task = PeriodicTask.objects.filter(enabled=True, last_run_at__isnull=False).order_by('-last_run_at').first()
        beat_info = {
            "status": "offline",
            "last_run": None
        }
        if recent_task and recent_task.last_run_at:
            if (timezone.now() - recent_task.last_run_at).total_seconds() < 300:
                beat_info["status"] = "online"
            beat_info["last_run"] = recent_task.last_run_at.isoformat()
            
        result = {
            "workers": worker_details,
            "queues": queue_stats,
            "beat": beat_info,
            "timestamp": timezone.now().isoformat()
        }
        
        # 写入长效缓存 (建议 2 分钟过期，防止任务挂掉后数据太陈旧)
        cache.set(cache_key, result, 120)
        logger.info("[Monitor] Celery stats collected and cached successfully.")
        return "Success"

    except Exception as e:
        logger.error(f"[Monitor] Failed to collect Celery stats: {str(e)}")
        return f"Error: {str(e)}"


@shared_task(name="apps.system_management.tasks.export_system_report_task")
def export_system_report_task(user_id, export_types, start_time_str, end_time_str, filters=None):
    import datetime
    import csv
    import uuid
    import os
    import tempfile
    import zipfile
    from django.conf import settings
    from django.utils import timezone
    from django.utils.dateparse import parse_datetime, parse_date
    from django.db.models import Count, Q
    
    # Models
    from apps.pipeline_management.models import PipelineRun
    from apps.task_management.models import AnsibleExecution, AnsibleTask
    from apps.host_management.models import ComplianceFramework, ComplianceClause, Host, Environment, Platform, ResourcePool
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

    if not filters:
        filters = {}

    project_id = filters.get('project_id')
    env_id = filters.get('env_id')
    platform_id = filters.get('platform_id')
    resource_pool_id = filters.get('resource_pool_id')

    # Ensure reports directory exists
    reports_dir = os.path.join(settings.MEDIA_ROOT, 'reports')
    os.makedirs(reports_dir, exist_ok=True)

    # Use a temp directory to write individual CSV files
    temp_dir = tempfile.mkdtemp()
    generated_files = []

    # 1. Pipeline Execution Report
    if 'pipeline' in export_types:
        runs = PipelineRun.objects.filter(create_time__range=(start_time, end_time))
        if project_id:
            runs = runs.filter(pipeline__project_id=project_id)
        
        filepath = os.path.join(temp_dir, 'pipeline_execution_report.csv')
        with open(filepath, 'w', encoding='utf-8-sig', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['流水线名称', '执行ID', '执行状态', '触发类型', '触发人', '开始时间', '结束时间', '耗时(秒)'])
            for run in runs:
                duration = 0
                if run.start_time and run.end_time:
                    duration = int((run.end_time - run.start_time).total_seconds())
                writer.writerow([
                    run.pipeline.name,
                    run.id,
                    run.get_status_display(),
                    run.get_trigger_type_display(),
                    run.trigger_user.username if run.trigger_user else '系统',
                    run.start_time.strftime('%Y-%m-%d %H:%M:%S') if run.start_time else '',
                    run.end_time.strftime('%Y-%m-%d %H:%M:%S') if run.end_time else '',
                    duration
                ])
        generated_files.append(('pipeline_execution_report.csv', filepath))

    # 2. Ansible Execution Report
    if 'ansible' in export_types:
        executions = AnsibleExecution.objects.filter(create_time__range=(start_time, end_time))
        if project_id:
            executions = executions.filter(task__project_id=project_id)
        if resource_pool_id:
            executions = executions.filter(task__resource_pool_id=resource_pool_id)
        if env_id:
            executions = executions.filter(task__resource_pool__hosts__env_id=env_id).distinct()
        if platform_id:
            executions = executions.filter(task__resource_pool__hosts__platform_id=platform_id).distinct()

        # Detailed Report
        filepath_detail = os.path.join(temp_dir, 'ansible_execution_detail.csv')
        with open(filepath_detail, 'w', encoding='utf-8-sig', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['任务名称', '执行ID', '执行状态', '执行者', '资源池', '关联项目', '开始时间', '结束时间', '耗时(秒)', '目标主机'])
            for ex in executions:
                duration = 0
                if ex.start_time and ex.end_time:
                    duration = int((ex.end_time - ex.start_time).total_seconds())
                hosts_str = ", ".join([h.hostname for h in ex.task.resource_pool.hosts.all()]) if ex.task.resource_pool else ''
                writer.writerow([
                    ex.task.name,
                    ex.id,
                    ex.get_status_display(),
                    ex.executor.username if ex.executor else '系统',
                    ex.task.resource_pool.name if ex.task.resource_pool else '',
                    ex.task.project.name if ex.task.project else '',
                    ex.start_time.strftime('%Y-%m-%d %H:%M:%S') if ex.start_time else '',
                    ex.end_time.strftime('%Y-%m-%d %H:%M:%S') if ex.end_time else '',
                    duration,
                    hosts_str
                ])
        generated_files.append(('ansible_execution_detail.csv', filepath_detail))

        # Dimensional Summary Report
        filepath_summary = os.path.join(temp_dir, 'ansible_execution_summary.csv')
        with open(filepath_summary, 'w', encoding='utf-8-sig', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['维度类别', '维度值', '总执行次数', '成功次数', '失败次数', '成功率 (%)'])
            
            # Env stats
            envs = Environment.objects.all()
            for env in envs:
                env_execs = executions.filter(task__resource_pool__hosts__env=env).distinct()
                total = env_execs.count()
                success = env_execs.filter(status='success').count()
                failed = env_execs.filter(status='failed').count()
                rate = round(success * 100.0 / total, 2) if total > 0 else 0.0
                writer.writerow(['环境', env.name, total, success, failed, f"{rate}%"])

            # Platform stats
            platforms = Platform.objects.all()
            for plat in platforms:
                plat_execs = executions.filter(task__resource_pool__hosts__platform=plat).distinct()
                total = plat_execs.count()
                success = plat_execs.filter(status='success').count()
                failed = plat_execs.filter(status='failed').count()
                rate = round(success * 100.0 / total, 2) if total > 0 else 0.0
                writer.writerow(['平台云厂商', plat.name or plat.get_type_display(), total, success, failed, f"{rate}%"])

            # Resource pool stats
            pools = ResourcePool.objects.all()
            for pool in pools:
                pool_execs = executions.filter(task__resource_pool=pool)
                total = pool_execs.count()
                success = pool_execs.filter(status='success').count()
                failed = pool_execs.filter(status='failed').count()
                rate = round(success * 100.0 / total, 2) if total > 0 else 0.0
                writer.writerow(['资源池', pool.name, total, success, failed, f"{rate}%"])
        generated_files.append(('ansible_execution_summary.csv', filepath_summary))

    # 3. Compliance Report
    if 'compliance' in export_types:
        filepath = os.path.join(temp_dir, 'compliance_status_report.csv')
        with open(filepath, 'w', encoding='utf-8-sig', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['合规框架', '条款编号', '条款名称', '条款描述', '合规状态', '关联主机基线', '所属资源池'])
            
            frameworks = ComplianceFramework.objects.all()
            for fw in frameworks:
                # Get all leaf clauses (clauses without children)
                clauses = ComplianceClause.objects.filter(framework=fw, children__isnull=True)
                for cl in clauses:
                    mappings = cl.baseline_mappings.all()
                    baselines_str = ", ".join([m.baseline.name for m in mappings])
                    pools_str = ", ".join([m.baseline.resource_pool.name for m in mappings if m.baseline.resource_pool])
                    
                    status_display = {
                        'success': '合规',
                        'failed': '不合规',
                        'running': '巡检中',
                        'pending': '未巡检'
                    }.get(cl.compliance_status, '未知')

                    writer.writerow([
                        fw.name,
                        cl.code,
                        cl.name,
                        cl.description or '',
                        status_display,
                        baselines_str,
                        pools_str
                    ])
        generated_files.append(('compliance_status_report.csv', filepath))

    # 4. SRE Alert Report
    if 'sre_alert' in export_types or 'alert' in export_types:
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

        filepath = os.path.join(temp_dir, 'sre_alert_report.csv')
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
        generated_files.append(('sre_alert_report.csv', filepath))

    # Build final package
    if len(generated_files) == 0:
        return "No reports selected"

    # Decide file format
    if len(generated_files) == 1:
        # If only one file was generated, copy it to media as a single CSV
        file_name, temp_filepath = generated_files[0]
        unique_filename = f"{os.path.splitext(file_name)[0]}_{uuid.uuid4().hex}.csv"
        dest_filepath = os.path.join(reports_dir, unique_filename)
        os.rename(temp_filepath, dest_filepath)
        file_url = f"{settings.MEDIA_URL}reports/{unique_filename}"
        title = "系统单项报表生成成功"
    else:
        # ZIP file
        unique_filename = f"system_report_package_{uuid.uuid4().hex}.zip"
        dest_filepath = os.path.join(reports_dir, unique_filename)
        with zipfile.ZipFile(dest_filepath, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            for arcname, filepath in generated_files:
                zip_file.write(filepath, arcname)
        file_url = f"{settings.MEDIA_URL}reports/{unique_filename}"
        title = "系统多维报表包（ZIP）生成成功"

    # Cleanup temp directory
    for _, filepath in generated_files:
        try:
            os.remove(filepath)
        except Exception:
            pass
    try:
        os.rmdir(temp_dir)
    except Exception:
        pass

    # Create UserNotification
    notification = UserNotification.objects.create(
        user_id=user_id,
        title=title,
        content=f"您导出的多维系统报表已于 {timezone.now().strftime('%Y-%m-%d %H:%M:%S')} 生成完毕，点击立即下载。",
        extra_data={"download_url": file_url, "type": "report_ready"}
    )

    # Broadcast WebSocket notification
    channel_layer = get_channel_layer()
    if channel_layer:
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

    return f"System report exported successfully: {file_url}"
