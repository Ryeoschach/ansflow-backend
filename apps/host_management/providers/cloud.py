import json
import logging
from typing import List, Dict, Any
from .base import BaseProvider

logger = logging.getLogger(__name__)

class AliyunProvider(BaseProvider):
    """
    阿里云资产同步适配器
    """
    def verify_connectivity(self) -> bool:
        try:
            from aliyunsdkcore.client import AcsClient
            from aliyunsdkecs.request.v20140526.DescribeRegionsRequest import DescribeRegionsRequest
            
            client = AcsClient(self.access_key, self.secret_key, self.api_endpoint or 'cn-hangzhou')
            request = DescribeRegionsRequest()
            client.do_action_with_exception(request)
            return True
        except Exception as e:
            self._set_error(f"阿里云验证失败: {str(e)}")
            return False

    def sync_assets(self) -> List[Dict[str, Any]]:
        try:
            from aliyunsdkcore.client import AcsClient
            from aliyunsdkecs.request.v20140526.DescribeInstancesRequest import DescribeInstancesRequest
            
            client = AcsClient(self.access_key, self.secret_key, self.api_endpoint or 'cn-hangzhou')
            request = DescribeInstancesRequest()
            request.set_PageSize(100)
            
            response = client.do_action_with_exception(request)
            data = json.loads(response)
            instances = data.get('Instances', {}).get('Instance', [])
            
            standardized_hosts = []
            for ins in instances:
                inner_ip_list = ins.get('VpcAttributes', {}).get('PrivateIpAddress', {}).get('IpAddress', [])
                private_ip = inner_ip_list[0] if inner_ip_list else (ins.get('InnerIpAddress', {}).get('IpAddress', [None])[0])
                
                if not private_ip: continue

                standardized_hosts.append({
                    'hostname': ins.get('InstanceName') or ins.get('InstanceId'),
                    'ip_address': ins.get('PublicIpAddress', {}).get('IpAddress', [None])[0],
                    'private_ip': private_ip,
                    'os_type': ins.get('OSName'),
                    'cpu': ins.get('Cpu'),
                    'memory': int(ins.get('Memory') / 1024),
                    'status': 1 if ins.get('Status') == 'Running' else 0,
                    'raw_data': ins # 保留原始数据以备后用
                })
            return standardized_hosts
        except Exception as e:
            self._set_error(f"阿里云同步失败: {str(e)}")
            return []

class AWSProvider(BaseProvider):
    """
    AWS 资产同步适配器
    """
    def verify_connectivity(self) -> bool:
        try:
            import boto3
            session = boto3.Session(
                aws_access_key_id=self.access_key,
                aws_secret_access_key=self.secret_key,
                region_name=self.api_endpoint or 'us-east-1'
            )
            sts = session.client('sts')
            sts.get_caller_identity()
            return True
        except Exception as e:
            self._set_error(f"AWS 验证失败: {str(e)}")
            return False

    def sync_assets(self) -> List[Dict[str, Any]]:
        # AWS 同步逻辑实现... (暂略，结构同阿里云)
        return []
