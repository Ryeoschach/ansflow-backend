import abc
import logging
from typing import List, Dict, Any

logger = logging.getLogger(__name__)

class BaseProvider(abc.ABC):
    """
    云平台服务商适配器基类
    """
    def __init__(self, access_key: str, secret_key: str, api_endpoint: str = None):
        self.access_key = access_key
        self.secret_key = secret_key
        self.api_endpoint = api_endpoint

    @abc.abstractmethod
    def verify_connectivity(self) -> bool:
        """
        验证连通性
        """
        pass

    @abc.abstractmethod
    def sync_assets(self) -> List[Dict[str, Any]]:
        """
        同步资产，返回标准化的主机信息列表
        """
        pass

    def get_error_message(self) -> str:
        return getattr(self, '_error_message', "")

    def _set_error(self, msg: str):
        self._error_message = msg
        logger.error(msg)
