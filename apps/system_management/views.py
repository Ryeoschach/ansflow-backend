import datetime
import os
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import AllowAny, IsAuthenticated
from .monitors import SystemHealthManager
from .notifiers import FeishuNotifier, DingTalkNotifier
from apps.host_management.models import Host, ResourcePool
from apps.task_management.models import AnsibleExecution
from apps.pipeline_management.models import PipelineRun
from django.utils import timezone
from datetime import timedelta
from django.db.models import Count, Q

class SystemHealthViewSet(viewsets.ViewSet):
    """
    系统健康状态视图集
    """
    def get_permissions(self):
        # 核心逻辑：允许匿名用户提交崩溃报告（ErrorBoundary 专用）
        if self.action == 'report_error':
            return [AllowAny()]
        return [IsAuthenticated()]
    
    @action(detail=False, methods=['get'])
    def status(self, request):
        """
        获取全系统组件健康概览
        """
        try:
            health_data = SystemHealthManager.get_all_health()
            
            # 计算总体状态
            overall = "healthy"
            if any(item['status'] == 'unhealthy' for item in health_data): overall = "critical"
            elif any(item['status'] == 'warning' for item in health_data): overall = "warning"
            
            return Response({
                "status": overall,
                "components": health_data,
                "timestamp": datetime.datetime.now().isoformat()
            })
        except Exception as e:
            return Response({"error": f"监控采集失败: {str(e)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=False, methods=['post'])
    def report_error(self, request):
        """
        接收前端 ErrorBoundary 提交的运行时崩溃报告
        """
        data = request.data
        error_msg = data.get('error', 'Unknown JS Error')
        stack_trace = data.get('stack', 'No stack trace provided')
        current_url = data.get('url', 'Unknown URL')
        
        # 飞书推送通知
        feishu_webhook = os.getenv('FEISHU_WEBHOOK')
        if feishu_webhook:
            title = "AnsFlow 前端运行时崩溃告警"
            content = (
                f"**错误信息**: {error_msg}\n"
                f"**页面地址**: {current_url}\n"
                f"**操作用户**: {request.user.username if request.user.is_authenticated else '匿名/未登录'}\n"
                f"**堆栈详情裁剪 (Top 5)**:\n{stack_trace[:500]}..."
            )
            FeishuNotifier(feishu_webhook).send(title, content, current_url)
            
        return Response({"status": "error_reported", "msg": "运维团队已收到崩溃信息"})

class DashboardViewSet(viewsets.ViewSet):
    """
    仪表盘概览数据视图
    """
    permission_classes = [IsAuthenticated]

    @action(detail=False, methods=['get'])
    def summary(self, request):
        now = timezone.now()
        last_24h = now - timedelta(hours=24)

        # 基础指标 (Metrics)
        total_hosts = Host.objects.count()
        online_hosts = Host.objects.filter(status=1).count()
        total_pools = ResourcePool.objects.count()
        
        # 任务执行统计 (24小时内)
        daily_executions = AnsibleExecution.objects.filter(create_time__gte=last_24h)
        daily_task_runs = daily_executions.count()
        daily_failed_tasks = daily_executions.filter(status='failed').count()

        # 任务趋势 (Task Trend - 每4小时一个点)
        task_trend = []
        for i in range(6):
            start = now - timedelta(hours=(6-i)*4)
            end = now - timedelta(hours=(5-i)*4)
            period_data = daily_executions.filter(create_time__gte=start, create_time__lt=end)
            
            task_trend.append({
                "time": start.strftime("%H:%M"),
                "success": period_data.filter(status='success').count(),
                "failed": period_data.filter(status='failed').count()
            })

        # 最近任务 (Recent Tasks - 混合 Ansible 和 Pipeline)
        ansible_recent = AnsibleExecution.objects.all().select_related('task', 'executor').order_by('-create_time')[:10]
        pipeline_recent = PipelineRun.objects.all().select_related('pipeline', 'trigger_user').order_by('-create_time')[:10]
        
        combined_recent = []
        for task in ansible_recent:
            combined_recent.append({
                "raw_id": task.id,
                "id": f"TSK-{task.id}",
                "type": "ansible",
                "name": task.task.name if task.task else "Unknown Task",
                "status": task.status.upper(),
                "time": task.create_time,
                "user": task.executor.username if task.executor else "System"
            })
        
        for run in pipeline_recent:
            combined_recent.append({
                "raw_id": run.id,
                "id": f"RUN-{run.id}",
                "type": "pipeline",
                "name": run.pipeline.name if run.pipeline else "Unknown Pipeline",
                "status": run.status.upper(),
                "time": run.create_time,
                "user": run.trigger_user.username if run.trigger_user else "System"
            })
            
        # 按时间排序并取前 8 条
        combined_recent.sort(key=lambda x: x['time'], reverse=True)
        final_recent = combined_recent[:8]

        for task in final_recent:
            # 简单的时间友好化处理
            delta = now - task['time']
            if delta.seconds < 3600:
                time_str = f"{delta.seconds // 60} mins ago"
            elif delta.days < 1:
                time_str = f"{delta.seconds // 3600} hours ago"
            else:
                time_str = f"{delta.days} days ago"
            task['time_label'] = time_str
            # 将 datetime 对象移除，因为 Response 不能序列化它，或者转为字符串
            task['time'] = task['time'].isoformat()

        return Response({
            "metrics": {
                "totalHosts": total_hosts,
                "onlineHosts": online_hosts,
                "totalResourcePools": total_pools,
                "dailyTaskRuns": daily_task_runs,
                "dailyFailedTasks": daily_failed_tasks,
            },
            "taskTrend": task_trend,
            "recentTasks": final_recent
        })
