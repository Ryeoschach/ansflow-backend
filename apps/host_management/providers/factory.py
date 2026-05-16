from typing import Type, Optional
from .base import BaseProvider
from .cloud import AliyunProvider, AWSProvider

class ProviderFactory:
    """
    云平台服务商工厂类
    """
    _providers = {
        'aliyun': AliyunProvider,
        'aws': AWSProvider,
        # 可以继续扩展腾讯云、VMware等
    }

    @classmethod
    def get_provider(cls, platform_type: str, access_key: str, secret_key: str, api_endpoint: str = None) -> Optional[BaseProvider]:
        provider_cls = cls._providers.get(platform_type)
        if provider_cls:
            return provider_cls(access_key, secret_key, api_endpoint)
        return None
