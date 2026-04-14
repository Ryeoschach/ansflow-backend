import logging
import random
from celery import shared_task
from django.utils import timezone
from apps.host_management.models import Platform

logger = logging.getLogger(__name__)

@shared_task(name="verify_platform_connectivity")
def verify_platform_connectivity(platform_id=None):
    """
    验证平台连通性
    如果不传 platform_id，则验证所有已启用的平台。
    """
    if platform_id:
        platforms = Platform.objects.filter(id=platform_id)
    else:
        platforms = Platform.objects.filter(status=True)

    for platform in platforms:
        try:
            logger.info(f"开始真实验证平台: {platform.name} ({platform.type})")
            
            if platform.type == 'aliyun':
                from aliyunsdkcore.client import AcsClient
                from aliyunsdkcore.acs_exception.exceptions import ClientException, ServerException
                from aliyunsdkecs.request.v20140526.DescribeRegionsRequest import DescribeRegionsRequest
                
                # 阿里云：尝试获取可用区列表
                client = AcsClient(platform.access_key, platform.secret_key, platform.api_endpoint or 'cn-hangzhou')
                request = DescribeRegionsRequest()
                try:
                    client.do_action_with_exception(request)
                    platform.connectivity_status = 1
                    platform.error_message = ""
                except (ClientException, ServerException) as e:
                    platform.connectivity_status = 2
                    platform.error_message = f"阿里云验证失败: {str(e)}"

            elif platform.type == 'aws':
                import boto3
                from botocore.exceptions import ClientError
                
                # AWS：尝试获取身份信息 (STS GetCallerIdentity)
                session = boto3.Session(
                    aws_access_key_id=platform.access_key,
                    aws_secret_access_key=platform.secret_key,
                    region_name=platform.api_endpoint or 'us-east-1'
                )
                sts = session.client('sts')
                try:
                    sts.get_caller_identity()
                    platform.connectivity_status = 1
                    platform.error_message = ""
                except ClientError as e:
                    platform.connectivity_status = 2
                    platform.error_message = f"AWS 验证失败: {str(e)}"

            # ... 其他逻辑类似 ...
            
            else:
                # 原有的 Socket 探测逻辑作为兜底 (针对本地机房或 K8s 端点)
                if platform.api_endpoint:
                    import socket
                    from urllib.parse import urlparse
                    endpoint = platform.api_endpoint
                    host = urlparse(endpoint).hostname or endpoint.split(':')[0]
                    port = 80 # 简化示例
                    socket.create_connection((host, port), timeout=5)
                    platform.connectivity_status = 1
                    platform.error_message = ""
                else:
                    platform.connectivity_status = 1 # 无配置默认正常

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
    具体的资产同步任务
    """
    from apps.host_management.models import Platform, Host, Environment
    platform = Platform.objects.get(id=platform_id)
    if platform.type != 'aliyun':
        return "目前仅支持阿里云自动同步"

    from aliyunsdkcore.client import AcsClient
    from aliyunsdkecs.request.v20140526.DescribeInstancesRequest import DescribeInstancesRequest
    import json

    client = AcsClient(platform.access_key, platform.secret_key, platform.api_endpoint or 'cn-hangzhou')
    
    # 简单的分页拉取示例
    request = DescribeInstancesRequest()
    request.set_PageSize(100)
    
    try:
        response = client.do_action_with_exception(request)
        data = json.loads(response)
        instances = data.get('Instances', {}).get('Instance', [])
        
        sync_count = 0
        # 默认找一个环境。
        default_env = Environment.objects.first()
        if not default_env:
            default_env = Environment.objects.create(name="默认环境", code="default")

        for ins in instances:
            inner_ip_list = ins.get('VpcAttributes', {}).get('PrivateIpAddress', {}).get('IpAddress', [])
            private_ip = inner_ip_list[0] if inner_ip_list else ""
            
            # 如果没有内网IP，尝试取 InnerIpAddress
            if not private_ip:
                inner_ips = ins.get('InnerIpAddress', {}).get('IpAddress', [])
                private_ip = inner_ips[0] if inner_ips else ""
            
            if not private_ip: continue

            # 更新或创建主机记录
            host, created = Host.objects.update_or_create(
                private_ip=private_ip,
                defaults={
                    'hostname': ins.get('InstanceName') or ins.get('InstanceId'),
                    'ip_address': ins.get('PublicIpAddress', {}).get('IpAddress', [None])[0],
                    'os_type': ins.get('OSName'),
                    'cpu': ins.get('Cpu'),
                    'memory': int(ins.get('Memory') / 1024),  # 转换成GB
                    'env': default_env,
                    'platform': platform,
                    'status': 1 if ins.get('Status') == 'Running' else 0
                }
            )
            if created: sync_count += 1

        return f"同步完成，新增 {sync_count} 台主机"
    except Exception as e:
        logger.error(f"同步资产失败: {str(e)}")
        raise e
