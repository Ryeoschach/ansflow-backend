from utils.base_model import BaseModel
from django.contrib.auth.models import AbstractUser
from django.db import models


# Create your models here.
class Permission(BaseModel):
    """
    权限项：定义了‘模块:资源:动作’
    """
    DANGER_LEVELS = [
        ('safe', '安全 (只读)'),
        ('warn', '警告 (写/创建)'),
        ('high', '危险 (删除/强制操作)'),
    ]

    name = models.CharField(max_length=100)
    # 唯一标识符，如 'k8s:pod:restart', 'city:data:delete'
    code = models.CharField(max_length=100, unique=True)
    module = models.CharField(max_length=50) # 所属模块
    desc = models.TextField(null=True, blank=True, verbose_name="描述")  # 记录这个标识符是哪个 View 产生的
    danger_level = models.CharField(
        max_length=10, choices=DANGER_LEVELS, default='safe', verbose_name="危险等级"
    )
    is_active = models.BooleanField(default=True, verbose_name="是否有效")
    is_manual = models.BooleanField(default=False, verbose_name="是否手动创建")

    def __str__(self):
        status = "" if self.is_active else "[已停用]"
        source = "[手动]" if self.is_manual else "[自动]"
        return f"{status}{source} {self.name} ({self.code})"

    class Meta:
        db_table = 'rbac_permission'


class Role(BaseModel):
    """
    角色：由于权限可能很多，所以通过角色来聚合
    """
    name = models.CharField(max_length=50, unique=True)
    code = models.CharField(max_length=50, unique=True, verbose_name="角色标识", default="", blank=True)
    permissions = models.ManyToManyField(Permission, blank=True)
    # 角色继承关系 ---
    # symmetrical=False 表示 A 继承 B，不代表 B 继承 A
    parents = models.ManyToManyField(
        'self',
        symmetrical=False,
        blank=True,
        related_name='children',
        verbose_name="继承自这些角色"
    )
    menus = models.ManyToManyField('Menu', blank=True, verbose_name="拥有的菜单")

    class Meta:
        db_table = 'rbac_permission_role'

    def __str__(self):
        return self.name

    def _get_all_ids(self, field_name, seen_roles=None, ids_set=None):
        """通用私有递归方法：收集关联对象的 ID 集合"""
        if seen_roles is None: seen_roles = set()
        if ids_set is None: ids_set = set()
        if self in seen_roles: return ids_set
        seen_roles.add(self)

        # 1. 收集当前角色直接关联的 ID
        for mid in getattr(self, field_name).values_list('id', flat=True):
            ids_set.add(mid)
        # 2. 递归收集父级角色的 ID
        for parent in self.parents.all():
            parent._get_all_ids(field_name, seen_roles, ids_set)
        return ids_set

    def get_all_permissions(self):
        """递归获取当前角色及其所有父辈角色的权限列表"""
        perm_ids = self._get_all_ids('permissions')
        return Permission.objects.filter(id__in=perm_ids, is_active=True)

    def get_all_menus(self):
        """递归获取当前角色及其所有父辈角色的菜单列表"""
        menu_ids = self._get_all_ids('menus')
        return Menu.objects.filter(id__in=menu_ids)

    def get_all_descendant_roles(self, seen_roles=None, ids_set=None):
        """递归获取所有继承了当前角色的子孙角色 (反向递归)"""
        if seen_roles is None: seen_roles = set()
        if ids_set is None: ids_set = set()
        if self in seen_roles: return ids_set
        seen_roles.add(self)

        for child in self.children.all():
            ids_set.add(child.id)
            child.get_all_descendant_roles(seen_roles, ids_set)
        
        return Role.objects.filter(id__in=ids_set)

    def get_all_data_policies(self, seen_roles=None):
        """
        递归获取当前角色及其所有父角色的数据授权策略
        返回格式：{ 'resource_type': set([id1, id2, ...]) }
        如果包含 "*", 则该类型为全选
        """
        if seen_roles is None: seen_roles = set()
        if self in seen_roles: return {}
        seen_roles.add(self)

        res = {}
        # 1. 处理自己的策略
        for policy in self.data_policies.all():
            rtype = policy.resource_type
            ids = set(policy.authorized_ids)
            if rtype not in res:
                res[rtype] = ids
            else:
                res[rtype] = res[rtype] | ids

        # 2. 递归合并父角色的策略
        for parent in self.parents.all():
            p_res = parent.get_all_data_policies(seen_roles)
            for rtype, ids in p_res.items():
                if rtype not in res:
                    res[rtype] = ids
                else:
                    res[rtype] = res[rtype] | ids
        
        return res


class User(AbstractUser):
    LOGIN_TYPE_CHOICES = [
        ('password', '密码登录'),
        ('ldap', 'LDAP 登录'),
        ('github', 'GitHub 授权'),
        ('wechat', '微信授权'),
    ]

    roles = models.ManyToManyField('rbac_permission.Role', blank=True)
    mobile = models.CharField(max_length=11, blank=True)
    avatar = models.ImageField(upload_to='avatars/', null=True, blank=True, verbose_name="头像")
    login_type = models.CharField(max_length=20, default='password', choices=LOGIN_TYPE_CHOICES, verbose_name="登录方式")
    github_id = models.CharField(max_length=100, blank=True, null=True, unique=True, verbose_name="GitHub ID")
    wechat_openid = models.CharField(max_length=100, blank=True, null=True, unique=True, verbose_name="微信 OpenID")
    ldap_dn = models.CharField(max_length=255, blank=True, null=True, verbose_name="LDAP DN")
    ldap_uid = models.CharField(max_length=128, blank=True, null=True, verbose_name="LDAP UID")

    class Meta:
        db_table = 'rbac_permission_user'


class DataPolicy(BaseModel):
    """
    数据权限策略：定义角色对具体资源实例的操作权限
    """
    RESOURCE_TYPES = (
        ('pipeline', '流水线'),
        ('k8s_cluster', 'K8s集群'),
        ('ansible_task', 'Ansible任务'),
        ('resource_pool', '资源池'),
        ('registry', '镜像仓库'),
        ('credential', 'SSH 凭据'),
    )

    ACTION_TYPES = (
        ('manage', '管理权限'),  # 包含 读、写、删
        ('use', '引用权限'),     # 仅允许在流水线等场景中作为参数“使用”
    )
    
    role = models.ForeignKey(Role, on_delete=models.CASCADE, related_name='data_policies')
    resource_type = models.CharField(max_length=50, choices=RESOURCE_TYPES)
    action_type = models.CharField(max_length=50, choices=ACTION_TYPES, default='manage', verbose_name="动作权限类型")
    # 存储具体的资源 ID 列表，如 [1, 2, 3] 或者 ["*"] 表示全部
    authorized_ids = models.JSONField(default=list, verbose_name="授权的资源ID列表")
    
    class Meta:
        db_table = 'rbac_data_policy'
        unique_together = ('role', 'resource_type', 'action_type')

    def __str__(self):
        return f"{self.role.name} - {self.get_resource_type_display()} ({self.get_action_type_display()})"


class Menu(BaseModel):
    """
    菜单模型：树形结构，管理前端展示
    """
    title = models.CharField(max_length=50, verbose_name="菜单标题")
    title_en = models.CharField(max_length=50, blank=True, verbose_name="菜单英文标题")
    key = models.CharField(max_length=50, unique=True, verbose_name="Antd菜单Key")
    path = models.CharField(max_length=200, verbose_name="路由路径")
    icon = models.CharField(max_length=50, blank=True, null=True, verbose_name="图标")
    parent = models.ForeignKey('self', on_delete=models.CASCADE, null=True, blank=True, related_name='children', verbose_name="父级菜单")
    order = models.IntegerField(default=0, verbose_name="排序")
    create_time = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.title

    class Meta:
        db_table = 'rbac_permission_menu'


class AuditLog(BaseModel):
    """
    审计日志：记录所有操作轨迹
    """
    user = models.ForeignKey('User', on_delete=models.SET_NULL, null=True, verbose_name="操作人")
    username = models.CharField(max_length=150, blank=True, verbose_name="用户名")  # 冗余字段，防止用户被删后无法溯源
    ip_address = models.GenericIPAddressField(verbose_name="操作IP", null=True, blank=True)
    method = models.CharField(max_length=10, verbose_name="请求方法")
    path = models.CharField(max_length=255, verbose_name="请求路径")

    # 业务维度
    resource = models.CharField(max_length=100, blank=True, verbose_name="资源Code")  # 如 'rbac:user'
    resource_name = models.CharField(max_length=100, blank=True, verbose_name="资源名称")  # 映射的可读名词
    action = models.CharField(max_length=50, blank=True, verbose_name="操作动作")  # 如 'create', 'update', 'delete'
    action_name = models.CharField(max_length=128, verbose_name='动作名称', null=True, blank=True)
    object_id = models.CharField(max_length=64, verbose_name='操作对象ID', null=True, blank=True)

    # 报文与响应
    old_data = models.JSONField(verbose_name='修改前旧主数据的快照', null=True, blank=True)
    request_data = models.JSONField(verbose_name='请求报文(新数据)', null=True, blank=True)
    response_data = models.JSONField(verbose_name='失败响应简报', null=True, blank=True)
    response_status = models.IntegerField(verbose_name='HTTP状态码')
    duration = models.FloatField(default=0.0, verbose_name="耗时(s)")

    class Meta:
        db_table = 'rbac_audit_log'
        ordering = ['-create_time']