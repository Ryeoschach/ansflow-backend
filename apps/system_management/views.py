import datetime
import os
import uuid
import gzip
import json
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from .monitors import SystemHealthManager
from .notifiers import FeishuNotifier, DingTalkNotifier
from apps.host_management.models import Host, ResourcePool
from apps.task_management.models import AnsibleExecution
from apps.pipeline_management.models import PipelineRun
from django.utils import timezone
from datetime import timedelta
from django.db import models
from django.db.models import Count, Q
from django.http import HttpResponse
from django.conf import settings

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
        from config import VERSION, BUILD_DATE

        try:
            health_data = SystemHealthManager.get_all_health()

            # 计算总体状态
            overall = "healthy"
            if any(item['status'] == 'unhealthy' for item in health_data): overall = "critical"
            elif any(item['status'] == 'warning' for item in health_data): overall = "warning"

            return Response({
                "status": overall,
                "version": VERSION,
                "build_date": BUILD_DATE,
                "components": health_data,
                "timestamp": datetime.datetime.now().isoformat()
            })
        except Exception as e:
            return Response({"error": f"监控采集失败: {str(e)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=False, methods=['get'])
    def celery_stats(self, request):
        """
        获取 Celery 详细监控统计信息 (纯只读缓存模式)
        响应时间固定 < 10ms，解决了广播同步等待导致的 30s+ 延迟。
        """
        from django.core.cache import cache
        cache_key = "ansflow:system:celery_stats"
        cached_data = cache.get(cache_key)

        if not cached_data:
            # 立即返回初始化状态，前端可据此展示“采集中”动画
            return Response({
                "workers": [],
                "queues": [],
                "beat": {"status": "initializing"},
                "message": "监控数据正在后台异步采集，请稍后刷新...",
                "timestamp": timezone.now().isoformat()
            })

        return Response(cached_data)


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
    仪表盘概览数据视图 (优化版)
    """
    permission_classes = [IsAuthenticated]

    @action(detail=False, methods=['get'])
    def summary(self, request):
        now = timezone.now()
        last_24h = now - timedelta(hours=24)

        # 1. 基础指标聚合查询 (Host 状态统计)
        host_stats = Host.objects.aggregate(
            total=models.Count('id'),
            online=models.Count('id', filter=models.Q(status=1))
        )
        
        # 2. 资产分布统计 (饼图数据)
        platform_dist = Host.objects.values('platform__name').annotate(count=models.Count('id')).order_by('-count')
        env_dist = Host.objects.values('env__name', 'env__color').annotate(count=models.Count('id')).order_by('-count')

        # 3. 任务执行统计
        daily_executions = AnsibleExecution.objects.filter(create_time__gte=last_24h)
        daily_task_runs = daily_executions.count()
        daily_failed_tasks = daily_executions.filter(status='failed').count()

        # 4. 任务趋势 (每4小时一个采样点)
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

        # 5. 实时告警数据
        from apps.sre_management.models import AlertEvent
        firing_alerts = AlertEvent.objects.filter(status='firing').order_by('-create_time')[:5].values(
            'id', 'alert_name', 'severity', 'create_time'
        )

        # 6. 最近动态 (混合排序与脱敏)
        ansible_recent = AnsibleExecution.objects.all().select_related('task', 'executor').order_by('-create_time')[:10]
        pipeline_recent = PipelineRun.objects.all().select_related('pipeline', 'trigger_user').order_by('-create_time')[:10]
        
        combined_recent = []
        for t in ansible_recent:
            combined_recent.append({
                "raw_id": t.id,
                "id": f"TSK-{t.id}",
                "type": "ansible",
                "name": t.task.name if t.task else "Ad-hoc Task",
                "status": t.status.upper(),
                "time": t.create_time,
                "user": t.executor.username if t.executor else "System"
            })
        
        for p in pipeline_recent:
            combined_recent.append({
                "raw_id": p.id,
                "id": f"RUN-{p.id}",
                "type": "pipeline",
                "name": p.pipeline.name if p.pipeline else "Unknown Pipeline",
                "status": p.status.upper(),
                "time": p.create_time,
                "user": p.trigger_user.username if p.trigger_user else "System"
            })
            
        # 按时间全量排序
        combined_recent.sort(key=lambda x: x['time'], reverse=True)
        final_recent = combined_recent[:10]

        for item in final_recent:
            # 时间友好化处理
            delta = now - item['time']
            if delta.total_seconds() < 60:
                time_str = "just now"
            elif delta.total_seconds() < 3600:
                time_str = f"{int(delta.total_seconds() // 60)}m ago"
            elif delta.days < 1:
                time_str = f"{int(delta.total_seconds() // 3600)}h ago"
            else:
                time_str = f"{delta.days}d ago"
            item['time_label'] = time_str
            item['time'] = item['time'].isoformat() # 序列化兼容

        return Response({
            "metrics": {
                "totalHosts": host_stats['total'] or 0,
                "onlineHosts": host_stats['online'] or 0,
                "totalResourcePools": ResourcePool.objects.count(),
                "dailyTaskRuns": daily_task_runs,
                "dailyFailedTasks": daily_failed_tasks,
            },
            "platformDistribution": [
                {"name": item['platform__name'] or "Local", "value": item['count']} for item in platform_dist
            ],
            "envDistribution": [
                {"name": item['env__name'] or "Unknown", "value": item['count'], "color": item['env__color']} for item in env_dist
            ],
            "firingAlerts": list(firing_alerts),
            "taskTrend": task_trend,
            "recentTasks": final_recent
        })


class BackupViewSet(viewsets.ViewSet):
    """
    系统备份与恢复视图
    """
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def _get_backup_dir(self):
        """获取备份存储目录"""
        backup_dir = os.path.join(settings.MEDIA_ROOT, 'backups')
        os.makedirs(backup_dir, exist_ok=True)
        return backup_dir

    def _get_selected_modules(self, data):
        """
        从 request.data 或 request.query_params 中解析并规范化模块列表
        """
        if not data:
            return None
        
        raw_val = None
        # 如果是 QueryDict，尝试 getlist
        if hasattr(data, 'getlist'):
            val_list = data.getlist('modules')
            if val_list:
                if len(val_list) > 1:
                    raw_val = val_list
                else:
                    raw_val = val_list[0]
                    
        if raw_val is None:
            raw_val = data.get('modules')

        if not raw_val:
            return None

        if isinstance(raw_val, list):
            return raw_val

        if isinstance(raw_val, str):
            raw_val = raw_val.strip()
            if not raw_val:
                return None
            # 兼容 JSON 数组格式，例如 '["rbac", "host"]'
            if raw_val.startswith('[') and raw_val.endswith(']'):
                try:
                    parsed = json.loads(raw_val)
                    if isinstance(parsed, list):
                        return parsed
                except Exception:
                    pass
            # 兼容逗号分隔格式，例如 "rbac,host"
            return [m.strip() for m in raw_val.split(',') if m.strip()]

        return None

    @action(detail=False, methods=['get'])
    def modules(self, request):
        """
        获取可备份/恢复的模块列表
        """
        from .backup import MODULE_DEFINITIONS
        return Response([
            {"key": k, "label": v['label']} for k, v in MODULE_DEFINITIONS.items()
        ])

    @action(detail=False, methods=['get', 'post'])
    def generate(self, request):
        """
        创建系统备份
        """
        from .backup import BackupExporter

        # 获取请求中的模块过滤列表和密码
        selected_modules = None
        passphrase = None
        if request.method == 'POST':
            selected_modules = self._get_selected_modules(request.data)
            passphrase = request.data.get('passphrase')
        else:
            selected_modules = self._get_selected_modules(request.query_params)
            passphrase = request.query_params.get('passphrase')

        if not passphrase:
            passphrase = None

        try:
            exporter = BackupExporter(passphrase=passphrase)
            backup_data = exporter.export(selected_modules=selected_modules)

            # 生成文件名
            timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
            suffix = "_modular" if selected_modules else "_full"
            filename = f'ansflow_backup_{timestamp}{suffix}.json.gz'
            file_path = os.path.join(self._get_backup_dir(), filename)

            # 写入压缩文件
            with gzip.open(file_path, 'wt', encoding='utf-8') as f:
                json.dump(backup_data, f, ensure_ascii=False, indent=2)

            # 返回文件路径（相对路径）
            file_url = f'/media/backups/{filename}'

            dt = datetime.datetime.strptime(timestamp, '%Y%m%d_%H%M%S')
            timestamp_display = dt.isoformat()

            return Response({
                'success': True,
                'filename': filename,
                'url': file_url,
                'size': os.path.getsize(file_path),
                'record_count': {k: len(v) for k, v in backup_data['data'].items()},
                'created_at': timestamp_display,
            })

        except Exception as e:
            return Response({
                'success': False,
                'error': f'备份创建失败: {str(e)}'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=False, methods=['get'])
    def index(self, request):
        """
        列出所有备份文件
        """
        backup_dir = self._get_backup_dir()
        backups = []

        for filename in os.listdir(backup_dir):
            if filename.endswith('.json.gz'):
                file_path = os.path.join(backup_dir, filename)
                stat = os.stat(file_path)
                # 尝试从文件名匹配时间戳 YYYYMMDD_HHMMSS
                import re
                match = re.search(r'(\d{8}_\d{6})', filename)
                if match:
                    timestamp = match.group(1)
                    try:
                        dt = datetime.datetime.strptime(timestamp, '%Y%m%d_%H%M%S')
                        timestamp_display = dt.isoformat()
                    except Exception:
                        dt = datetime.datetime.fromtimestamp(stat.st_mtime)
                        timestamp_display = dt.isoformat()
                else:
                    dt = datetime.datetime.fromtimestamp(stat.st_mtime)
                    timestamp_display = dt.isoformat()

                backups.append({
                    'filename': filename,
                    'url': f'/media/backups/{filename}',
                    'size': stat.st_size,
                    'created_at': timestamp_display,
                })

        # 按时间倒序
        backups.sort(key=lambda x: x['created_at'], reverse=True)
        return Response(backups)

    @action(detail=False, methods=['get'])
    def download(self, request):
        """
        下载指定备份文件
        """
        filename = request.query_params.get('filename')
        if not filename:
            return Response({'error': '缺少 filename 参数'}, status=status.HTTP_400_BAD_REQUEST)

        # 安全检查：只允许下载 ansflow_backup_ 开头的文件
        if not filename.startswith('ansflow_backup_') and not filename.startswith('uploaded_'):
            return Response({'error': '非法文件名'}, status=status.HTTP_403_FORBIDDEN)

        file_path = os.path.join(self._get_backup_dir(), filename)
        if not os.path.exists(file_path):
            return Response({'error': '备份文件不存在'}, status=status.HTTP_404_NOT_FOUND)

        # 手动读取文件内容，通过 DRF Response 返回，以便通过认证
        with open(file_path, 'rb') as f:
            content = f.read()

        from django.http import HttpResponse
        response = HttpResponse(content, content_type='application/octet-stream')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response

    @action(detail=False, methods=['post'])
    def restore(self, request):
        """
        从备份文件恢复数据
        """
        from .backup import BackupImporter

        filename = request.data.get('filename')
        selected_modules = self._get_selected_modules(request.data)
        passphrase = request.data.get('passphrase')
        if not passphrase:
            passphrase = None
        
        if not filename:
            return Response({'error': '缺少 filename 参数'}, status=status.HTTP_400_BAD_REQUEST)

        file_path = os.path.join(self._get_backup_dir(), filename)
        if not os.path.exists(file_path):
            return Response({'error': '备份文件不存在'}, status=status.HTTP_404_NOT_FOUND)

        try:
            importer = BackupImporter({}, passphrase=passphrase)
            result = importer.import_from_file(file_path, selected_modules=selected_modules)

            return Response({
                'success': result['success'],
                'imported': result['imported'],
                'errors': result['errors'],
            })

        except Exception as e:
            return Response({
                'success': False,
                'error': f'恢复失败: {str(e)}'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=False, methods=['post'])
    def upload(self, request):
        """
        上传备份文件并恢复
        """
        from .backup import BackupImporter

        file = request.FILES.get('file')
        selected_modules = self._get_selected_modules(request.data)
        passphrase = request.data.get('passphrase')
        if not passphrase:
            passphrase = None
        
        if not file:
            return Response({'error': '缺少备份文件'}, status=status.HTTP_400_BAD_REQUEST)

        if not file.name.endswith('.json.gz'):
            return Response({'error': '只支持 .json.gz 格式的备份文件'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            # 保存上传文件
            timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f'uploaded_{timestamp}_{file.name}'
            file_path = os.path.join(self._get_backup_dir(), filename)

            with open(file_path, 'wb') as f:
                for chunk in file.chunks():
                    f.write(chunk)

            # 执行恢复
            importer = BackupImporter({}, passphrase=passphrase)
            result = importer.import_from_file(file_path, selected_modules=selected_modules)

            # 删除临时上传文件
            os.remove(file_path)

            return Response({
                'success': result['success'],
                'imported': result['imported'],
                'errors': result['errors'],
            })

        except Exception as e:
            return Response({
                'success': False,
                'error': f'上传恢复失败: {str(e)}'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=False, methods=['post'])
    def delete(self, request):
        """
        删除备份文件
        """
        filenames = request.data.get('filenames', [])
        if not filenames:
            return Response({'error': '缺少 filenames 参数'}, status=status.HTTP_400_BAD_REQUEST)

        if not isinstance(filenames, list):
            return Response({'error': 'filenames 必须是数组'}, status=status.HTTP_400_BAD_REQUEST)

        deleted = []
        errors = []
        for filename in filenames:
            # 安全检查
            if not filename.startswith('ansflow_backup_') and not filename.startswith('uploaded_'):
                errors.append(f'非法文件名: {filename}')
                continue

            file_path = os.path.join(self._get_backup_dir(), filename)
            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                    deleted.append(filename)
                except Exception as e:
                    errors.append(f'{filename}: {str(e)}')
            else:
                errors.append(f'{filename}: 文件不存在')

        return Response({
            'success': len(errors) == 0,
            'deleted': deleted,
            'errors': errors,
        })

from rest_framework import viewsets
from rest_framework.decorators import action
from django_celery_beat.models import PeriodicTask, IntervalSchedule, CrontabSchedule
from .serializers import PeriodicTaskSerializer
from utils.rbac_permission import SmartRBACPermission
from utils.pagination import MyCustomPagination
import json

class PeriodicTaskViewSet(viewsets.ModelViewSet):
    """
    通用系统级定时任务管理
    """
    queryset = PeriodicTask.objects.all().order_by('-id')
    serializer_class = PeriodicTaskSerializer
    permission_classes = [SmartRBACPermission]
    pagination_class = MyCustomPagination
    resource_code = "system:periodic_tasks"
    
    def get_queryset(self):
        return super().get_queryset()

    @action(detail=True, methods=['put'])
    def update_schedule(self, request, pk=None):
        task = self.get_object()
        data = request.data
        
        if 'args' in data:
            task.args = data['args']
        if 'kwargs' in data:
            task.kwargs = data['kwargs']
            
        schedule_type = data.get('schedule_type')
        if schedule_type == 'interval':
            every = data.get('every', 1)
            period = data.get('period', 'seconds')
            interval, _ = IntervalSchedule.objects.get_or_create(every=every, period=period)
            task.interval = interval
            task.crontab = None
        elif schedule_type == 'crontab':
            crontab_data = data.get('crontab', {})
            minute = crontab_data.get('minute', '*')
            hour = crontab_data.get('hour', '*')
            day_of_week = crontab_data.get('day_of_week', '*')
            day_of_month = crontab_data.get('day_of_month', '*')
            month_of_year = crontab_data.get('month_of_year', '*')
            crontab, _ = CrontabSchedule.objects.get_or_create(
                minute=minute, hour=hour, day_of_week=day_of_week, 
                day_of_month=day_of_month, month_of_year=month_of_year
            )
            task.crontab = crontab
            task.interval = None
            
        task.save()
        return Response({'status': 'success'})
