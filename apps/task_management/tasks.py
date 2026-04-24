import os
import shutil
import logging
from celery import shared_task
import ansible_runner
from django.utils import timezone
from apps.task_management.models import AnsibleTask, AnsibleExecution, TaskLog
from apps.task_management.utils import generate_ansible_inventory

logger = logging.getLogger(__name__)

@shared_task(bind=True, name="run_ansible_task")
def run_ansible_task(self, execution_id, extra_vars=None):
    """
    异步执行 Ansible 任务实例，支持传入外部变量
    """
    try:
        execution = AnsibleExecution.objects.select_related('task').get(id=execution_id)
        task = execution.task
        
        execution.status = 'running'
        execution.start_time = timezone.now()
        execution.celery_task_id = self.request.id
        execution.save()
        
        # 准备 Inventory
        inventory = generate_ansible_inventory(task.resource_pool_id)

        # 获取资源池信息（用于生成组名）
        from apps.host_management.models import ResourcePool
        pool = ResourcePool.objects.get(id=task.resource_pool_id)

        # 创建临时工作目录
        private_data_dir = f'/tmp/ansible_execution_{execution_id}'
        if os.path.exists(private_data_dir):
            shutil.rmtree(private_data_dir)
        os.makedirs(private_data_dir, exist_ok=True)

        # 处理 SSH 私钥
        all_children = inventory.get("all", {}).get("children", {})
        for g_name, g_data in all_children.items():
            hosts_dict = g_data.get("hosts", {})
            for host, vars in hosts_dict.items():
                if vars.get("_auth_type") == "key" and vars.get("_private_key"):
                    key_path = os.path.join(private_data_dir, f"key_{host}")
                    # OpenSSH 非常严格要求私钥必须以换行符结尾，且最好不包含 Windows 的 \r
                    clean_key = vars["_private_key"].replace('\r\n', '\n').strip() + '\n'
                    with open(key_path, "w") as f:
                        f.write(clean_key)
                    os.chmod(key_path, 0o600)
                    vars["ansible_ssh_private_key_file"] = key_path
                    vars.pop("_private_key", None)
                    vars.pop("_auth_type", None)
        
        # 准备启动参数
        runner_kwargs = {
            'private_data_dir': private_data_dir,
            'inventory': inventory,
            'envvars': {
                'ANSIBLE_HOST_KEY_CHECKING': 'False',
                'ANSIBLE_STDOUT_CALLBACK': 'default',
                'ANSIBLE_NOCOLOR': 'True',
                'FORCE_COLOR': '0',
            }
        }
        
        if extra_vars:
            runner_kwargs['extravars'] = extra_vars
        
        # 组名增加前缀避免与主机名冲突
        group_key = f"pool_{pool.code}"
        runner_kwargs['host_pattern'] = group_key

        if task.task_type == 'cmd':
            runner_kwargs['module'] = 'shell'
            runner_kwargs['module_args'] = task.content
        else:
            # 将 playbook 中的 `- hosts: localhost` 替换为实际的资源池组名
            playbook_content = task.content
            if '- hosts: localhost' in playbook_content:
                playbook_content = playbook_content.replace('- hosts: localhost', f'- hosts: {group_key}')
            elif '- hosts: all' in playbook_content:
                playbook_content = playbook_content.replace('- hosts: all', f'- hosts: {group_key}')

            playbook_path = os.path.join(private_data_dir, 'project', 'playbook.yml')
            os.makedirs(os.path.dirname(playbook_path), exist_ok=True)
            with open(playbook_path, 'w') as f:
                f.write(playbook_content)
            runner_kwargs['playbook'] = 'playbook.yml'

        # 定义事件回调
        def event_handler(event):
            event_type = event.get('event')
            stdout = event.get('stdout', '')
            if not stdout or not str(stdout).strip():
                return

            event_data = event.get('event_data', {})
            host = event_data.get('host')

            if host:
                res = event_data.get('res', {})
                detailed = res.get('stdout') or res.get('msg') or ""
                final_log = stdout.strip()
                if detailed and str(detailed).strip() not in final_log:
                    final_log += f"\n{str(detailed).strip()}"

                TaskLog.objects.create(
                    execution=execution, 
                    host=host, 
                    output=final_log
                )
            elif event_type == 'playbook_on_stats':
                TaskLog.objects.create(
                    execution=execution, 
                    host="SUMMARY", 
                    output=stdout.strip()
                )
            else:
                TaskLog.objects.create(
                    execution=execution, 
                    host="SYSTEM", 
                    output=stdout.strip()
                )

        # 处理超时时间 (ansible-runner 支持 timeout 秒数)
        runner_kwargs['timeout'] = task.timeout
        
        # 同步执行
        r = ansible_runner.run(**runner_kwargs, event_handler=event_handler)
        
        # 获取最终格式化日志 (从 TaskLog 获取以保持一致性)
        logs = TaskLog.objects.filter(execution=execution).order_by('create_time')
        formatted_logs = "\n".join([f"[{l.host}] {l.output}" for l in logs])

        # 更新状态
        execution.status = 'success' if r.rc == 0 else 'failed'
        execution.result_summary = r.stats
        execution.end_time = timezone.now()
        execution.save()

        # 发送执行结果通知
        from apps.system_management.notifiers import notify_task_result
        notify_task_result(execution)

        return {
            "status": execution.status,
            "logs": formatted_logs,
            "msg": f"实例 {execution_id} 执行完成"
        }
        
    except Exception as e:
        logger.error(f"执行实例 {execution_id} 出错: {str(e)}")
        if 'execution' in locals():
            execution.status = 'failed'
            execution.remark = f"内部错误: {str(e)}"
            execution.save()
            # 发送执行结果通知
            from apps.system_management.notifiers import notify_task_result
            notify_task_result(execution)
        return f"实例 {execution_id} 失败: {str(e)}"


@shared_task(name="run_ansible_schedule")
def run_ansible_schedule(schedule_id):
    """
    执行定时调度任务
    """
    from apps.task_management.models import AnsibleSchedule
    try:
        schedule = AnsibleSchedule.objects.select_related('task', 'creator').get(id=schedule_id)
        task = schedule.task

        # 创建执行记录
        execution = AnsibleExecution.objects.create(
            task=task,
            executor=schedule.creator,
            status='pending'
        )

        extra_vars = {}
        if isinstance(task.extra_vars, dict):
            extra_vars = task.extra_vars
        elif isinstance(task.extra_vars, str) and task.extra_vars.strip():
            import json
            try:
                extra_vars = json.loads(task.extra_vars)
            except json.JSONDecodeError:
                pass

        res = run_ansible_task.delay(execution.id, extra_vars)
        execution.celery_task_id = res.id
        execution.save()

        return {"status": "triggered", "execution_id": execution.id}
    except AnsibleSchedule.DoesNotExist:
        logger.error(f"Schedule {schedule_id} not found")
        return f"Schedule {schedule_id} not found"
    except Exception as e:
        logger.error(f"Schedule {schedule_id} error: {str(e)}")
        return f"Schedule {schedule_id} error: {str(e)}"


def sync_schedule_to_beat(schedule):
    """
    将调度同步到 Celery Beat
    """
    from django_celery_beat.models import PeriodicTask, IntervalSchedule, CrontabSchedule
    import croniter
    from datetime import datetime

    if not schedule.is_enabled:
        # 如果调度被禁用，删除关联的 PeriodicTask
        if schedule.periodic_task_id:
            try:
                PeriodicTask.objects.get(id=schedule.periodic_task_id).delete()
            except PeriodicTask.DoesNotExist:
                pass
            schedule.periodic_task_id = None
            schedule.save(update_fields=['periodic_task_id'])
        return

    # 创建或更新 IntervalSchedule
    if schedule.schedule_type == 'interval':
        interval_map = {
            'minutes': IntervalSchedule.MINUTES,
            'hours': IntervalSchedule.HOURS,
            'days': IntervalSchedule.DAYS,
        }
        interval_schedule, _ = IntervalSchedule.objects.get_or_create(
            every=schedule.interval_value,
            period=interval_map.get(schedule.interval_unit, IntervalSchedule.HOURS)
        )
        task = PeriodicTask.objects.update_or_create(
            id=schedule.periodic_task_id if schedule.periodic_task_id else None,
            defaults={
                'name': f"ansible_schedule_{schedule.id}",
                'task': 'run_ansible_schedule',
                'interval': interval_schedule,
                'args': json.dumps([schedule.id]),
                'enabled': True,
            }
        )[0]
    else:  # custom cron
        # 解析 cron 表达式: 分 时 日 月 周
        parts = (schedule.cron_expression or '0 * * * *').split()
        if len(parts) != 5:
            logger.error(f"Invalid cron expression: {schedule.cron_expression}")
            return

        cron_schedule, _ = CrontabSchedule.objects.get_or_create(
            minute=parts[0],
            hour=parts[1],
            day_of_month=parts[2],
            month_of_year=parts[3],
            day_of_week=parts[4],
        )
        task = PeriodicTask.objects.update_or_create(
            id=schedule.periodic_task_id if schedule.periodic_task_id else None,
            defaults={
                'name': f"ansible_schedule_{schedule.id}",
                'task': 'run_ansible_schedule',
                'crontab': cron_schedule,
                'args': json.dumps([schedule.id]),
                'enabled': True,
            }
        )[0]

    # 计算下次执行时间
    try:
        cron = croniter.croniter(schedule.cron_expression, datetime.now())
        schedule.next_run_time = datetime.fromtimestamp(cron.get_next())
    except:
        schedule.next_run_time = None

    schedule.periodic_task_id = task.id
    schedule.save(update_fields=['periodic_task_id', 'next_run_time'])
