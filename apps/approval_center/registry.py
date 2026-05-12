import logging

logger = logging.getLogger(__name__)

class ApprovalResourceRegistry:
    """
    审批拦截资源注册中心：允许各业务模块动态注册其支持的拦截行为，并同步至数据库。
    """
    _resources = {}

    @classmethod
    def register(cls, code, name, name_en=None, icon="PartitionOutlined", description="", description_en=""):
        """
        注册一个可拦截资源（同步内存并尝试持久化到 DB）
        """
        cls._resources[code] = {
            "code": code,
            "name": name,
            "name_en": name_en,
            "icon": icon,
            "description": description,
            "description_en": description_en
        }
        
        # 尝试将资源同步到数据库
        try:
            from .models import ApprovalResource
            # 使用 update_or_create 确保系统内置资源的展示信息是最新的
            # 但不强制更新 is_active，保留用户的手动配置
            obj, created = ApprovalResource.objects.update_or_create(
                code=code,
                defaults={
                    "name": name,
                    "name_en": name_en,
                    "icon": icon,
                    "description": description,
                    "description_en": description_en,
                    "is_system": True
                }
            )
            if created:
                logger.info(f"[ApprovalRegistry] New resource discovered and persisted: {code}")
        except Exception as e:
            # 兼容迁移阶段数据库表不存在的情况
            logger.warning(f"[ApprovalRegistry] Could not persist resource {code} to DB: {str(e)}")

    @classmethod
    def get_all_resources(cls):
        """返回所有已注册的资源列表"""
        return list(cls._resources.values())

# 全局单例
approval_registry = ApprovalResourceRegistry()
