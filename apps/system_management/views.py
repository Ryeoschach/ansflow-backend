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

    @action(detail=False, methods=['get'])
    def generate(self, request):
        """
        创建系统全量备份
        """
        from .backup import BackupExporter

        try:
            exporter = BackupExporter()
            backup_data = exporter.export()

            # 生成文件名
            timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f'ansflow_backup_{timestamp}.json.gz'
            file_path = os.path.join(self._get_backup_dir(), filename)

            # 写入压缩文件
            with gzip.open(file_path, 'wt', encoding='utf-8') as f:
                json.dump(backup_data, f, ensure_ascii=False, indent=2)

            # 返回文件路径（相对路径）
            file_url = f'/media/backups/{filename}'

            return Response({
                'success': True,
                'filename': filename,
                'url': file_url,
                'size': os.path.getsize(file_path),
                'record_count': {k: len(v) for k, v in backup_data['data'].items()},
                'created_at': timestamp,
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
                # 从文件名提取时间戳
                timestamp = filename.replace('ansflow_backup_', '').replace('.json.gz', '')
                try:
                    dt = datetime.datetime.strptime(timestamp, '%Y%m%d_%H%M%S')
                    timestamp_display = dt.strftime('%Y-%m-%d %H:%M:%S')
                except Exception:
                    timestamp_display = timestamp

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
        if not filename:
            return Response({'error': '缺少 filename 参数'}, status=status.HTTP_400_BAD_REQUEST)

        file_path = os.path.join(self._get_backup_dir(), filename)
        if not os.path.exists(file_path):
            return Response({'error': '备份文件不存在'}, status=status.HTTP_404_NOT_FOUND)

        try:
            importer = BackupImporter({})
            result = importer.import_from_file(file_path)

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
            importer = BackupImporter({})
            result = importer.import_from_file(file_path)

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
