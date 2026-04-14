import inspect
from django.core.management.base import BaseCommand
from django.apps import apps
from apps.rbac_permission.models import Permission

# ============================================================
# 全局默认动作语义表（作为 fallback）
# ============================================================
DEFAULT_ACTION_LABELS = {
    'view':     {'name': '查看列表/详情', 'danger': 'safe'},
    'add':      {'name': '新增',          'danger': 'warn'},
    'edit':     {'name': '编辑',          'danger': 'warn'},
    'delete':   {'name': '删除',          'danger': 'high'},
    'create':   {'name': '创建',          'danger': 'warn'},
    'update':   {'name': '修改',          'danger': 'warn'},
    'retrieve': {'name': '查看详情',      'danger': 'safe'},
    'list':     {'name': '查看列表',      'danger': 'safe'},
    'destroy':  {'name': '销毁/删除',     'danger': 'high'},
    'partial_update': {'name': '局部修改', 'danger': 'warn'},
    'run':       {'name': '触发执行',     'danger': 'warn'},
    'execute':   {'name': '执行',         'danger': 'warn'},
    'stop':      {'name': '停止',         'danger': 'high'},
    'terminate': {'name': '强制终止',     'danger': 'high'},
    'restart':   {'name': '重启',         'danger': 'warn'},
    'verify':    {'name': '连通性验证',   'danger': 'safe'},
}


class Command(BaseCommand):
    help = "同步权限：支持逻辑删除和手动权限保护，优先读取 View 上的 permission_labels"

    def handle(self, *args, **options):
        self.stdout.write(self.style.WARNING("--- 开始权限审计（语义增强模式） ---"))

        base_actions = ['view', 'add', 'edit', 'delete']
        code_in_codebase = set()

        # 1. 扫描代码并同步
        for app_config in apps.get_app_configs():
            if app_config.name.startswith(('django.', 'rest_framework')):
                continue

            try:
                views_module = __import__(f"{app_config.name}.views", fromlist=[''])
            except ImportError:
                continue

            for name, obj in inspect.getmembers(views_module, inspect.isclass):
                # 1. 识别资源标识 (支持 list 形式的 resource_codes)
                resources = getattr(obj, 'resource_codes', [])
                if not resources:
                    res = getattr(obj, 'resource_code', None)
                    if isinstance(res, str):
                        resources = [res]
                    elif not res:
                        continue

                module_base_name = app_config.verbose_name or app_config.name

                # 2. 读取 View 上的语义标签声明（最高优先级）
                # 格式: permission_labels = {
                #   'stop': {'name': '强制停止流水线', 'danger': 'high', 'desc': '...'},
                # }
                view_labels = getattr(obj, 'permission_labels', {})

                # 3. 组合所有动作
                current_view_actions = base_actions.copy()
                for attr_name in dir(obj):
                    try:
                        attr = getattr(obj, attr_name)
                        if hasattr(attr, 'mapping'):
                            if attr_name not in current_view_actions:
                                current_view_actions.append(attr_name)
                    except Exception:
                        continue

                # 4. 为每个资源标识生成全量动作权限
                for resource in resources:
                    display_module = module_base_name
                    if 'helm' in resource:
                        display_module = "Helm 应用中心"
                    elif 'k8s' in resource:
                        display_module = "K8s 集群管理"

                    for act in current_view_actions:
                        # 过滤掉不属于该模块的特定 action
                        if 'helm' in resource and not act.startswith(('helm_', 'chart_')) and act not in base_actions:
                            continue
                        if 'k8s' in resource and act.startswith(('helm_', 'chart_')):
                            continue

                        code = f"{resource}:{act}"
                        code_in_codebase.add(code)

                        # ===================================================
                        # 语义标签优先级：
                        # 1. View.permission_labels[act]
                        # 2. DEFAULT_ACTION_LABELS[act]
                        # 3. Fallback: raw class-act name
                        # ===================================================
                        if act in view_labels:
                            label_cfg = view_labels[act]
                        elif act in DEFAULT_ACTION_LABELS:
                            label_cfg = DEFAULT_ACTION_LABELS[act]
                        else:
                            label_cfg = {'name': act.replace('_', ' ').title(), 'danger': 'warn'}

                        perm_name = label_cfg.get('name', act)
                        danger = label_cfg.get('danger', 'safe')
                        desc = label_cfg.get('desc', f"模块: {display_module} | 资源: {resource} | 动作: {act}")

                        # 如果是 View 显式声明的，则不覆盖 name/danger（避免手动调整后被冲掉）
                        # 如果权限已存在且是手动的，跳过覆盖
                        existing = Permission.objects.filter(code=code).first()
                        if existing and existing.is_manual:
                            code_in_codebase.add(code)
                            continue

                        Permission.objects.update_or_create(
                            code=code,
                            defaults={
                                'name': perm_name,
                                'module': display_module,
                                'desc': desc,
                                'danger_level': danger,
                                'is_active': True,
                                'is_manual': False
                            }
                        )
                        self.stdout.write(f"  [{'VIEW' if act in view_labels else 'AUTO'}] {code} → {perm_name} ({danger})")

        # 5. 处理"多出来"的权限（逻辑删除）
        deprecated_qs = Permission.objects.filter(
            is_manual=False,
            is_active=True
        ).exclude(code__in=code_in_codebase)

        count = deprecated_qs.count()
        if count > 0:
            deprecated_qs.update(is_active=False)
            self.stdout.write(self.style.NOTICE(f"  [-] 已逻辑删除 {count} 项过期自动权限"))

        manual_count = Permission.objects.filter(is_manual=True).count()
        self.stdout.write(
            self.style.SUCCESS(f"--- 同步完成！有效自动权限: {len(code_in_codebase)}, 手动保留权限: {manual_count} ---"))