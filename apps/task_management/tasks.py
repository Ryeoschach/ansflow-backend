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
                    with open(key_path, "w") as f:
                        f.write(vars["_private_key"])
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
        
        if task.task_type == 'cmd':
            runner_kwargs['module'] = 'shell'
            runner_kwargs['module_args'] = task.content
            runner_kwargs['host_pattern'] = 'all'
        else:
            playbook_path = os.path.join(private_data_dir, 'project', 'playbook.yml')
            os.makedirs(os.path.dirname(playbook_path), exist_ok=True)
            with open(playbook_path, 'w') as f:
                f.write(task.content)
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
        return f"实例 {execution_id} 失败: {str(e)}"
