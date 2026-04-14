from drf_spectacular.openapi import AutoSchema
from utils.rbac_permission import SmartRBACPermission, RBAC_ACTION_MAP

class RBACAutoSchema(AutoSchema):
    def get_description(self):
        # 获取原有的描述（从 @extend_schema 注解来的）
        description = super().get_description() or ""

        # 判断当前视图是否使用了 SmartRBACPermission
        permissions = getattr(self.view, 'permission_classes', [])
        if SmartRBACPermission in permissions:
            # 尝试提取资源码
            resource = getattr(self.view, 'resource_code', None)
            if resource:
                # 获取当前请求动作，如果在 ViewSet 中通常是 action，否则是 method
                # drf_spectacular 处理 method 时直接使用 self.method
                action = getattr(self.view, 'action', None) or self.method.lower()

                # RBAC 权限映射表
                perm_action = RBAC_ACTION_MAP.get(action, action)

                # perm_action = action_map.get(action, action)
                if not perm_action:
                    perm_action = 'unknown'

                required_code = f"{resource}:{perm_action}"

                # 将所需权限自动拼接进文档描述结尾
                rbac_notice = f"\n\n> **[RBAC 权限]**: 需拥有 `{required_code}` (或对应继承/通配符权限) 才能访问。"
                description += rbac_notice

        return description
