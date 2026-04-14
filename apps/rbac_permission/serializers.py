from apps.rbac_permission.models import Permission, Role, User, Menu, DataPolicy, AuditLog
from rest_framework import serializers


class PermissionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Permission
        fields = '__all__'
        extra_kwargs = {
            'module': {'required': False, 'allow_blank': True, 'default': '手动创建模块'},
        }

    def create(self, validated_data):
        # 确保前端不传 module 时有个默认的分类
        if 'module' not in validated_data or not validated_data['module']:
            validated_data['module'] = '手动创建模块'
        
        # 通过前端 API 接口即人工建立的，必须强制保证是手动权限
        # 防止再次运行同步脚本被当作「系统未使用废弃项」给自动删除了
        validated_data['is_manual'] = True 
        return super().create(validated_data)


class RoleSerializer(serializers.ModelSerializer):
    data_policies = serializers.SerializerMethodField()

    class Meta:
        model = Role
        fields = '__all__'
        extra_kwargs = {
            'permissions': {'required': False},
            'menus': {'required': False},
        }

    def get_data_policies(self, obj):
        # 按照 { 资源: { 动作: [ID列表] } } 格式返回给前端
        res = {}
        for p in obj.data_policies.all():
            if p.resource_type not in res:
                res[p.resource_type] = {}
            res[p.resource_type][p.action_type] = p.authorized_ids
        return res

class DataPolicySerializer(serializers.ModelSerializer):
    class Meta:
        model = DataPolicy
        fields = '__all__'

class UserSerializer(serializers.ModelSerializer):
    password = serializers.CharField(
        write_only=True,
        required=True,
        style={'input_type': 'password'}
    )
    roles_info = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ['id', 'username', 'password', 'email', 'roles', 'roles_info', 'is_active', 'is_staff']

    def get_roles_info(self, obj):
        # 返回角色的 ID 和 名称，供前端表格渲染 Tag
        return [{"id": r.id, "name": r.name} for r in obj.roles.all()]

    # 因为是继承Django的User表，需要重写create来处理密码
    def create(self, validated_data):
        # 弹出密码，单独处理
        password = validated_data.pop('password')
        # 弹出 roles (多对多需要单独处理)
        roles = validated_data.pop('roles', [])

        # 使用 create_user 方法（自动处理 salt 加密）
        user = User.objects.create_user(**validated_data)
        user.set_password(password)  # 再次确保加密
        user.save()

        # 处理多对多关系
        if roles:
            user.roles.set(roles)

        return user

    # 修改用户需要重写 update 方法
    def update(self, instance, validated_data):
        password = validated_data.pop('password', None)
        if password:
            instance.set_password(password)
        return super().update(instance, validated_data)


class MenuSerializer(serializers.ModelSerializer):
    """
    菜单序列化器：支持树形结构递归显示
    """
    # 使用 serializers.SerializerMethodField 或递归嵌套来处理子菜单
    children = serializers.SerializerMethodField()

    class Meta:
        model = Menu
        fields = ('id', 'title', 'key', 'path', 'icon', 'parent', 'order', 'children', 'create_time')
        read_only_fields = ('create_time',)

    def get_children(self, obj):
        """
        递归获取子菜单，按 order 字段排序，并根据权限过滤
        """
        children_queryset = obj.children.all().order_by('order')

        # 获取 context 中的限制（在 my_menus action 中注入）
        menu_ids = self.context.get('menu_ids')
        is_superuser = self.context.get('is_superuser', False)

        if menu_ids is not None and not is_superuser:
            # 如果提供了权限列表且不是超级管理员，则过滤子菜单
            children_queryset = children_queryset.filter(id__in=menu_ids)

        if children_queryset.exists():
            # 传递 context 确保子级递归也能拿到限制
            return MenuSerializer(children_queryset, many=True, context=self.context).data
        return []


class AuditLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = AuditLog
        fields = '__all__'