import json
import os
import shutil
import logging
from datetime import datetime
from celery import shared_task
from croniter import croniter
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
    # 兼容直接调用：如果第一个参数不是 Task 实例（比如在 pipeline 中直接调用），则进行参数位移
    if not hasattr(self, 'request'):
        extra_vars = execution_id
        execution_id = self
        self = None

    try:
        execution = AnsibleExecution.objects.select_related('task').get(id=execution_id)
        task = execution.task
        
        execution.status = 'running'
        execution.start_time = timezone.now()
        if self:
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
            'forks': getattr(task, 'forks', 5), # 动态获取并发数
            'envvars': {
                'ANSIBLE_HOST_KEY_CHECKING': 'False',
                'ANSIBLE_STDOUT_CALLBACK': 'default',
                'ANSIBLE_NOCOLOR': 'True',
                'FORCE_COLOR': '0',
            }
        }
        
        # 1. 从配置中心加载全局变量，用于注入到 Ansible 的 extra_vars
        from utils.config_manager import ConfigCache
        global_extra_vars = {}
        try:
            configs = ConfigCache.get_all_configs()
            for category, items in configs.items():
                for key, val in items.items():
                    # 转换值：对于非简单类型，保留原样；支持裸 key、下划线拼接、点号拼接形式
                    global_extra_vars[key] = val
                    global_extra_vars[f"{category}_{key}"] = val
                    global_extra_vars[f"{category}.{key}"] = val
        except Exception as e:
            logger.error(f"加载配置中心全局变量失败: {e}")

        # 2. 合并传入的外部变量与配置中心变量
        combined_extra_vars = {}
        combined_extra_vars.update(global_extra_vars)
        if extra_vars:
            combined_extra_vars.update(extra_vars)

        if combined_extra_vars:
            runner_kwargs['extravars'] = combined_extra_vars
        
        # 组名增加前缀避免与主机名冲突
        group_key = f"pool_{pool.code}"
        runner_kwargs['host_pattern'] = group_key

        # 3. 替换内容中的 ${var} 占位符
        # 并在 ad-hoc (cmd) 模式下，也替换 {{ var }} 占位符，因为 ad-hoc 无法被 ansible 原生渲染
        resolved_content = task.content or ""
        if resolved_content:
            flat_str_vars = {}
            for k, v in combined_extra_vars.items():
                # 只有非字典和非列表的标量，才转为字符串用于文本正则替换
                if not isinstance(v, (dict, list)):
                    flat_str_vars[k] = str(v)
            
            import re
            def _replace(match):
                var_name = match.group(1).strip()
                var_name_clean = var_name.replace('.', '_')
                if var_name in flat_str_vars:
                    return flat_str_vars[var_name]
                elif var_name_clean in flat_str_vars:
                    return flat_str_vars[var_name_clean]
                return match.group(0)

            # 替换 ${key}
            pattern_dollar = r"\$\{\s*([\w\.\-_]+)\s*\}"
            resolved_content = re.sub(pattern_dollar, _replace, resolved_content)

            # 如果是 ad-hoc cmd 类型，也替换 {{ key }}
            if task.task_type == 'cmd':
                pattern_jinja = r"\{\{\s*([\w\.\-_]+)\s*\}\}"
                resolved_content = re.sub(pattern_jinja, _replace, resolved_content)

        if task.task_type == 'cmd':
            runner_kwargs['module'] = 'shell'
            runner_kwargs['module_args'] = resolved_content
        else:
            # 将 playbook 中的 `- hosts: localhost` 替换为实际的资源池组名
            playbook_content = resolved_content
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
        logger.info(f"开始执行 ansible-runner: execution_id={execution_id}")
        r = ansible_runner.run(**runner_kwargs, event_handler=event_handler)
        
        # 获取最终格式化日志
        logs = TaskLog.objects.filter(execution=execution).order_by('create_time')
        formatted_logs = "\n".join([f"[{l.host}] {l.output}" for l in logs])

        # 更新状态：严格根据 ansible-runner 的返回码判定
        final_status = 'success' if r.rc == 0 else 'failed'
        logger.info(f"Ansible 执行结束: rc={r.rc}, status={final_status}")
        
        execution.status = final_status
        execution.result_summary = r.stats
        execution.end_time = timezone.now()
        execution.save()

        # 发送执行结果通知
        try:
            from apps.system_management.notifiers import notify_task_result
            notify_task_result(execution)
        except Exception as e:
            logger.error(f"发送通知失败: {e}")

        return {
            "status": final_status,
            "logs": formatted_logs,
            "msg": f"实例 {execution_id} 执行完成"
        }
        
    except Exception as e:
        import traceback
        logger.error(f"执行实例 {execution_id} 产生致命错误: {str(e)}\n{traceback.format_exc()}")
        if 'execution' in locals():
            execution.status = 'failed'
            execution.remark = f"内部错误: {str(e)}"
            execution.save()
        return {
            "status": "failed",
            "msg": str(e)
        }


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
            try:
                extra_vars = json.loads(task.extra_vars)
            except json.JSONDecodeError:
                pass

        res = run_ansible_task.delay(execution.id, extra_vars)
        execution.celery_task_id = res.id
        execution.save()

        # 执行后更新 next_run_time
        update_next_run_time(schedule)

        return {"status": "triggered", "execution_id": execution.id}
    except AnsibleSchedule.DoesNotExist:
        logger.error(f"Schedule {schedule_id} not found")
        return f"Schedule {schedule_id} not found"
    except Exception as e:
        logger.error(f"Schedule {schedule_id} error: {str(e)}")
        return f"Schedule {schedule_id} error: {str(e)}"


def update_next_run_time(schedule):
    """
    更新调度的下次执行时间
    """
    try:
        if schedule.schedule_type == 'cron' and schedule.cron_expression:
            cron = croniter(schedule.cron_expression, timezone.now())
            schedule.next_run_time = datetime.fromtimestamp(cron.get_next())
        elif schedule.schedule_type == 'interval':
            from datetime import timedelta
            unit_map = {'minutes': 60, 'hours': 3600, 'days': 86400}
            seconds = schedule.interval_value * unit_map.get(schedule.interval_unit, 3600)
            schedule.next_run_time = timezone.now() + timedelta(seconds=seconds)
        else:
            schedule.next_run_time = None
        schedule.save(update_fields=['next_run_time'])
    except Exception as e:
        logger.error(f"Failed to update next_run_time: {e}")


def sync_schedule_to_beat(schedule):
    """
    将调度同步到 Celery Beat
    """
    from django_celery_beat.models import PeriodicTask, IntervalSchedule, CrontabSchedule

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
        cron_expr = schedule.cron_expression or '0 * * * *'
        parts = cron_expr.split()
        if len(parts) != 5:
            logger.error(f"Invalid cron expression: {schedule.cron_expression}, using default '0 * * * *'")
            cron_expr = '0 * * * *'
            parts = cron_expr.split()

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
    update_next_run_time(schedule)

    schedule.periodic_task_id = task.id
    schedule.save(update_fields=['periodic_task_id', 'next_run_time'])
