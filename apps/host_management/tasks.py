import logging
import random
from celery import shared_task
from django.utils import timezone
from apps.host_management.models import Platform, Host, Environment
from apps.host_management.providers.factory import ProviderFactory

logger = logging.getLogger(__name__)

@shared_task(name="verify_platform_connectivity")
def verify_platform_connectivity(platform_id=None):
    """
    验证平台连通性 (重构后：使用适配器模式)
    """
    if platform_id:
        platforms = Platform.objects.filter(id=platform_id)
    else:
        platforms = Platform.objects.filter(status=True)

    for platform in platforms:
        try:
            logger.info(f"开始验证平台: {platform.name} ({platform.type})")
            provider = ProviderFactory.get_provider(
                platform.type, 
                platform.access_key, 
                platform.secret_key, 
                platform.api_endpoint
            )

            if provider:
                if provider.verify_connectivity():
                    platform.connectivity_status = 1
                    platform.error_message = ""
                else:
                    platform.connectivity_status = 2
                    platform.error_message = provider.get_error_message()
            else:
                # 兜底：原始 Socket 探测逻辑 (针对本地机房或 K8s 端点)
                if platform.api_endpoint:
                    import socket
                    from urllib.parse import urlparse
                    endpoint = platform.api_endpoint
                    host = urlparse(endpoint).hostname or endpoint.split(':')[0]
                    port = 80
                    socket.create_connection((host, port), timeout=5)
                    platform.connectivity_status = 1
                    platform.error_message = ""
                else:
                    platform.connectivity_status = 1

            platform.last_verified_at = timezone.now()
            platform.save()
            
        except Exception as e:
            logger.error(f"验证平台 {platform.name} 出错: {str(e)}")
            platform.connectivity_status = 2
            platform.error_message = str(e)
            platform.last_verified_at = timezone.now()
            platform.save()

    return f"验证完成，处理了 {platforms.count()} 个平台"


@shared_task(name="sync_platform_assets")
def sync_platform_assets(platform_id):
    """
    具体的资产同步任务 (重构后：使用适配器模式)
    """
    platform = Platform.objects.get(id=platform_id)
    provider = ProviderFactory.get_provider(
        platform.type, 
        platform.access_key, 
        platform.secret_key, 
        platform.api_endpoint
    )

    if not provider:
        return f"平台类型 {platform.type} 不支持自动同步"

    try:
        hosts_data = provider.sync_assets()
        if not hosts_data:
            return f"同步完成，未发现新主机或获取失败: {provider.get_error_message()}"
        
        sync_count = 0
        default_env = Environment.objects.first()
        if not default_env:
            default_env = Environment.objects.create(name="默认环境", code="default")

        for h_info in hosts_data:
            host, created = Host.objects.update_or_create(
                private_ip=h_info['private_ip'],
                defaults={
                    'hostname': h_info['hostname'],
                    'ip_address': h_info.get('ip_address'),
                    'os_type': h_info.get('os_type', 'Linux'),
                    'cpu': h_info.get('cpu', 1),
                    'memory': h_info.get('memory', 1),
                    'env': default_env,
                    'platform': platform,
                    'status': h_info.get('status', 1)
                }
            )
            if created: sync_count += 1

        return f"同步完成，新增 {sync_count} 台主机"
    except Exception as e:
        logger.error(f"同步资产失败: {str(e)}")
        raise e

@shared_task(name="check_host_connectivity")
def check_host_connectivity():
    """
    定期检查所有标记为“在线”的主机的 SSH 连通性
    """
    from apps.host_management.models import Host
    import paramiko
    import io

    hosts = Host.objects.filter(status=1) # 仅检查在线主机
    checked_count = 0
    fail_count = 0

    for host in hosts:
        # 获取最有效的凭据 (主机特定凭据 > 平台默认凭据)
        credential = host.credential or (host.platform.default_credential if host.platform else None)
        if not credential:
            continue

        checked_count += 1
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        try:
            if credential.auth_type == 'password':
                client.connect(
                    hostname=host.private_ip or host.ip_address,
                    port=22,
                    username=credential.username,
                    password=credential.password,
                    timeout=5
                )
            else:
                # 处理私钥
                key_stream = io.StringIO(credential.private_key)
                if 'BEGIN RSA PRIVATE KEY' in credential.private_key:
                    pkey = paramiko.RSAKey.from_private_key(key_stream, password=credential.passphrase)
                elif 'BEGIN OPENSSH PRIVATE KEY' in credential.private_key:
                    pkey = paramiko.Ed25519Key.from_private_key(key_stream, password=credential.passphrase)
                else:
                    pkey = paramiko.RSAKey.from_private_key(key_stream, password=credential.passphrase)

                client.connect(
                    hostname=host.private_ip or host.ip_address,
                    port=22,
                    username=credential.username,
                    pkey=pkey,
                    timeout=5
                )
            client.close()
            # 探测成功，保持在线
            if host.status != 1:
                host.status = 1
                host.save(update_fields=['status', 'update_time'])
        except Exception as e:
            logger.warning(f"主机 {host.hostname} ({host.private_ip}) 连通性探测失败: {str(e)}")
            # 探测失败，标记为故障 (status=2)
            host.status = 2
            host.save(update_fields=['status', 'update_time'])
            fail_count += 1

    return f"主机心跳检查完成。共检查 {checked_count} 台，失败 {fail_count} 台。"

@shared_task(name="check_host_baseline")
def check_host_baseline(baseline_id):
    """
    定期巡检物理机基线：
    1. 使用 Ansible 运行 check_playbook
    2. 如果失败，创建告警事件
    3. 如果开启 auto_remediate，尝试运行 remediate_playbook
    """
    from .models import HostBaseline
    from apps.task_management.models import AnsibleTask, AnsibleExecution
    from apps.task_management.tasks import run_ansible_task
    from apps.sre_management.models import AlertEvent
    import yaml

    baseline = HostBaseline.objects.get(id=baseline_id)
    if not baseline.is_active:
        return "Baseline inactive"

    # 1. 创建巡检任务
    check_task = AnsibleTask.objects.create(
        name=f"BaselineCheck_{baseline.name}_{timezone.now().strftime('%Y%m%d%H%M')}",
        task_type='playbook',
        resource_pool=baseline.resource_pool,
        content=baseline.check_playbook,
        creator=None, # 系统触发
        create_type='system'
    )

    execution = AnsibleExecution.objects.create(
        task=check_task,
        status='pending',
        from_pipeline=True
    )
    
    # 更新基线为巡检中
    baseline.last_check_status = 'running'
    baseline.last_execution_id = execution.id
    baseline.save(update_fields=['last_check_status', 'last_execution_id'])

    try:
        # 2. 执行巡检
        result = run_ansible_task(execution.id)
        # 无论成功失败，都记录本次执行的时间
        baseline.last_check_time = timezone.now()
        baseline.last_check_status = result.get('status', 'failed') if result else 'failed'
        baseline.save()
    except Exception as run_err:
        logger.error(f"Ansible execution fatal error: {str(run_err)}")
        baseline.last_check_status = 'failed'
        baseline.save()
        return f"Baseline check failed: {str(run_err)}"

    if result.get('status') == 'failed':
        # 3. 产生告警
        alert = AlertEvent.objects.create(
            alert_name=f"基线巡检失败: {baseline.name}",
            alert_level='critical',
            service_name=baseline.resource_pool.name,
            alert_content=f"资源池 {baseline.resource_pool.name} 未通过基线检查 {baseline.name}。\n日志摘要:\n{result.get('logs', '')[:1000]}",
            status='active'
        )

        # 4. 自动修复
        if baseline.auto_remediate and baseline.remediate_playbook:
            logger.info(f"触发基线自动修复: {baseline.name}")
            fix_task = AnsibleTask.objects.create(
                name=f"BaselineRemediate_{baseline.name}_{timezone.now().strftime('%Y%m%d%H%M')}",
                task_type='playbook',
                resource_pool=baseline.resource_pool,
                content=baseline.remediate_playbook,
                creator=None,
                create_type='system'
            )
            fix_exec = AnsibleExecution.objects.create(task=fix_task, status='pending', from_pipeline=True)
            run_ansible_task.delay(fix_exec.id)
            # 标记告警为正在处理
            alert.status = 'acknowledged'
            alert.save()

    return f"Baseline {baseline.name} check finished with status: {result.get('status')}"

