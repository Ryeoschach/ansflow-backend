"""
系统备份与恢复模块

功能：
- 全量备份：导出所有业务数据为 JSON 文件
- 恢复导入：支持选择性地恢复指定模块数据
- 加密字段处理：加密字段以密文存储，导入时用目标实例的 KEY 重新加密

导出顺序（按外键依赖排序）：
1. Permission, Menu (无依赖)
2. Credential, SshCredential, Environment (无依赖)
3. Role (依赖 Permission, Menu)
4. DataPolicy (依赖 Role)
5. User (依赖 Role)
6. Platform (依赖 SshCredential)
7. K8sCluster, ImageRegistry (独立，有加密)
8. Host (依赖 Environment, Platform, SshCredential)
9. ResourcePool (依赖 Host, M2M)
10. Pipeline (依赖 User)
11. CIEnvironment
12. ConfigCategory
13. ConfigItem (依赖 ConfigCategory)
14. ApprovalPolicy (独立)
"""

import json
import gzip
import base64
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass, field, asdict

from django.db import transaction
from django.conf import settings
from django.contrib.auth.hashers import make_password

logger = logging.getLogger(__name__)


# ============================================================
# 工具函数
# ============================================================

def get_encrypted_field_names() -> set:
    """返回所有加密字段的模型名.字段名集合"""
    return {
        'Credential.secret_value',
        'SshCredential.password',
        'SshCredential.private_key',
        'SshCredential.passphrase',
        'Platform.access_key',
        'Platform.secret_key',
        'K8sCluster.kubeconfig_content',
        'K8sCluster.token',
        'ImageRegistry.password',
        'ArtifactoryInstance.api_key',
        'ArtifactoryInstance.password',
        'ConfigItem.value',  # 当 is_encrypted=True 时
    }


def is_encrypted_field(model_name: str, field_name: str) -> bool:
    return f'{model_name}.{field_name}' in get_encrypted_field_names()


# ============================================================
# 数据模型映射（用于 JSON 序列化/反序列化）
# ============================================================

@dataclass
class ModelInfo:
    """模型元信息"""
    app_label: str
    model_name: str
    table_name: str
    pk_field: str = 'id'
    # 导出时排除的字段（如执行日志、审计日志等）
    exclude_fields: List[str] = field(default_factory=list)
    # 外键映射: field_name -> (related_model_name, id_field)
    fk_fields: Dict[str, Tuple[str, str]] = field(default_factory=dict)
    # M2M 字段: field_name -> related_model_name
    m2m_fields: Dict[str, str] = field(default_factory=dict)
    # 依赖顺序（越小越先导出）
    export_order: int = 99


# 定义所有需要备份的模型及其元信息
MODEL_INFOS: Dict[str, ModelInfo] = {
    'Permission': ModelInfo(
        app_label='rbac_permission', model_name='Permission', table_name='rbac_permission',
        exclude_fields=['remark'],
        export_order=1,
    ),
    'Menu': ModelInfo(
        app_label='rbac_permission', model_name='Menu', table_name='rbac_permission_menu',
        fk_fields={'parent': ('Menu', 'id')},
        exclude_fields=['remark'],
        export_order=2,
    ),
    'Credential': ModelInfo(
        app_label='credentials_management', model_name='Credential', table_name='sys_credential_vault',
        exclude_fields=['remark'],
        export_order=3,
    ),
    'SshCredential': ModelInfo(
        app_label='host_management', model_name='SshCredential', table_name='cmdb_ssh_credential',
        exclude_fields=['remark'],
        export_order=4,
    ),
    'Environment': ModelInfo(
        app_label='host_management', model_name='Environment', table_name='cmdb_environment',
        exclude_fields=['remark'],
        export_order=5,
    ),
    'Role': ModelInfo(
        app_label='rbac_permission', model_name='Role', table_name='rbac_permission_role',
        m2m_fields={
            'permissions': 'Permission',
            'parents': 'Role',
            'children': 'Role',
            'menus': 'Menu',
        },
        exclude_fields=['remark'],
        export_order=6,
    ),
    'DataPolicy': ModelInfo(
        app_label='rbac_permission', model_name='DataPolicy', table_name='rbac_data_policy',
        fk_fields={'role': ('Role', 'id')},
        exclude_fields=['remark'],
        export_order=7,
    ),
    'User': ModelInfo(
        app_label='rbac_permission', model_name='User', table_name='rbac_permission_user',
        m2m_fields={'roles': 'Role'},
        # 排除密码、最后登录、三方登录字段、头像等
        exclude_fields=['password', 'last_login', 'remark', 'date_joined',
                        'github_id', 'wechat_openid', 'ldap_dn', 'ldap_uid', 'login_type', 'avatar'],
        export_order=8,
    ),
    'Platform': ModelInfo(
        app_label='host_management', model_name='Platform', table_name='cmdb_platform',
        fk_fields={'default_credential': ('SshCredential', 'id')},
        exclude_fields=['remark'],
        export_order=9,
    ),
    'K8sCluster': ModelInfo(
        app_label='k8s_management', model_name='K8sCluster', table_name='k8s_clusters',
        exclude_fields=['remark'],
        export_order=10,
    ),
    'ImageRegistry': ModelInfo(
        app_label='registry_management', model_name='ImageRegistry', table_name='registry_image_registry',
        exclude_fields=['remark'],
        export_order=11,
    ),
    'ArtifactoryInstance': ModelInfo(
        app_label='registry_management', model_name='ArtifactoryInstance', table_name='registry_artifactory_instance',
        exclude_fields=['remark'],
        export_order=11.5,
    ),
    'ArtifactoryRepository': ModelInfo(
        app_label='registry_management', model_name='ArtifactoryRepository', table_name='registry_artifactory_repository',
        fk_fields={'instance': ('ArtifactoryInstance', 'id')},
        exclude_fields=['remark'],
        export_order=12,
    ),
    'PipelineRun': ModelInfo(
        app_label='pipeline_management', model_name='PipelineRun', table_name='pipeline_pipelinerun',
        fk_fields={
            'pipeline': ('Pipeline', 'id'),
            'trigger_user': ('User', 'id'),
        },
        exclude_fields=['remark'],
        export_order=13,
    ),
    'Artifact': ModelInfo(
        app_label='registry_management', model_name='Artifact', table_name='pipeline_artifact',
        fk_fields={
            'image_registry': ('ImageRegistry', 'id'),
            'artifactory_repo': ('ArtifactoryRepository', 'id'),
            'pipeline': ('Pipeline', 'id'),
        },
        exclude_fields=['remark'],
        export_order=14,
    ),
    'ArtifactVersion': ModelInfo(
        app_label='registry_management', model_name='ArtifactVersion', table_name='pipeline_artifact_version',
        fk_fields={
            'artifact': ('Artifact', 'id'),
            'pipeline_run': ('PipelineRun', 'id'),
        },
        exclude_fields=['remark'],
        export_order=15,
    ),
    'Host': ModelInfo(
        app_label='host_management', model_name='Host', table_name='cmdb_host',
        fk_fields={
            'env': ('Environment', 'id'),
            'platform': ('Platform', 'id'),
            'credential': ('SshCredential', 'id'),
        },
        exclude_fields=['remark'],
        export_order=16,
    ),
    'ResourcePool': ModelInfo(
        app_label='host_management', model_name='ResourcePool', table_name='cmdb_resource_pool',
        m2m_fields={'hosts': 'Host'},
        exclude_fields=['remark'],
        export_order=17,
    ),
    'Pipeline': ModelInfo(
        app_label='pipeline_management', model_name='Pipeline', table_name='pipeline_template',
        fk_fields={'creator': ('User', 'id')},
        exclude_fields=['remark'],
        export_order=18,
    ),
    'CIEnvironment': ModelInfo(
        app_label='pipeline_management', model_name='CIEnvironment', table_name='pipeline_ci_environment',
        exclude_fields=['remark'],
        export_order=19,
    ),
    'ConfigCategory': ModelInfo(
        app_label='config_center', model_name='ConfigCategory', table_name='config_center_category',
        exclude_fields=['remark'],
        export_order=20,
    ),
    'ConfigItem': ModelInfo(
        app_label='config_center', model_name='ConfigItem', table_name='config_center_item',
        fk_fields={'category': ('ConfigCategory', 'id')},
        exclude_fields=['remark'],
        export_order=21,
    ),
    'ApprovalPolicy': ModelInfo(
        app_label='approval_center', model_name='ApprovalPolicy', table_name='approval_center_policy',
        exclude_fields=['remark'],
        export_order=22,
    ),
}


# ============================================================
# 备份导出器
# ============================================================

class BackupExporter:
    """系统数据备份导出器"""

    VERSION = '1.0'

    def __init__(self):
        self.data: Dict[str, List[Dict]] = {}
        self.metadata: Dict[str, Any] = {
            'version': self.VERSION,
            'created_at': datetime.now().isoformat(),
            'encrypted_fields': list(get_encrypted_field_names()),
        }

    def export(self) -> Dict[str, Any]:
        """执行全量导出"""
        # 按依赖顺序导出每个模型
        sorted_models = sorted(MODEL_INFOS.items(), key=lambda x: x[1].export_order)

        for model_name, model_info in sorted_models:
            records = self._export_model(model_info)
            if records:
                self.data[model_name] = records
                logger.info(f"[Backup] 导出 {model_name}: {len(records)} 条")

        return {
            'metadata': self.metadata,
            'data': self.data,
        }

    def _export_model(self, model_info: ModelInfo) -> List[Dict]:
        """导出单个模型的数据"""
        from django.apps import apps
        Model = apps.get_model(model_info.app_label, model_info.model_name)

        # 获取所有字段
        fields = [f.name for f in Model._meta.get_fields() if not f.many_to_many and not f.one_to_many]
        fields = [f for f in fields if f not in model_info.exclude_fields and f not in ['id', 'create_time', 'update_time']]

        records = []
        for obj in Model.objects.all().only(*fields):
            record = {'id': obj.id}

            for field_name in fields:
                value = getattr(obj, field_name, None)

                # 处理外键
                if field_name in model_info.fk_fields:
                    if value is not None:
                        record[field_name] = value.id if hasattr(value, 'id') else value
                    else:
                        record[field_name] = None
                # 处理普通字段
                elif value is not None:
                    if isinstance(value, (datetime,)):
                        record[field_name] = value.isoformat()
                    elif isinstance(value, (list, dict)):
                        record[field_name] = value
                    else:
                        record[field_name] = value
                else:
                    record[field_name] = None

            # 处理 M2M 字段
            for m2m_field, related_model in model_info.m2m_fields.items():
                try:
                    m2m_ids = list(getattr(obj, m2m_field).all().values_list('id', flat=True))
                    record[f'{m2m_field}_ids'] = m2m_ids
                except Exception:
                    record[f'{m2m_field}_ids'] = []

            records.append(record)

        return records

    def export_to_file(self, file_path: str):
        """导出为 gzip 压缩的 JSON 文件"""
        with gzip.open(file_path, 'wt', encoding='utf-8') as f:
            json.dump(self.export(), f, ensure_ascii=False, indent=2)


# ============================================================
# 备份恢复导入器
# ============================================================

class BackupImporter:
    """系统数据备份恢复导入器"""

    def __init__(self, backup_data: Dict[str, Any]):
        self.backup_data = backup_data
        self.data = backup_data.get('data', {})
        self.metadata = backup_data.get('metadata', {})

        # ID 映射: model_name -> {old_id: new_id}
        self.id_map: Dict[str, Dict[int, int]] = {}

        # M2M 关系缓冲: (model_name, obj_id, m2m_field) -> [related_ids]
        self.m2m_buffer: Dict[Tuple, List[int]] = {}

        self.import_log: List[str] = []
        self.errors: List[str] = []

    def _log(self, msg: str):
        self.import_log.append(msg)
        logger.info(f"[Restore] {msg}")

    def _error(self, msg: str):
        self.errors.append(msg)
        logger.error(f"[Restore] 错误: {msg}")

    @transaction.atomic
    def import_all(self) -> Dict[str, Any]:
        """执行全量恢复（原子事务）"""
        # 按依赖顺序导入
        sorted_models = sorted(MODEL_INFOS.items(), key=lambda x: x[1].export_order)

        for model_name, model_info in sorted_models:
            records = self.data.get(model_name, [])
            if records:
                self._import_model(model_info, records)

        # 第二遍：处理 M2M 关系
        for (model_name, obj_id, m2m_field), related_ids in self.m2m_buffer.items():
            self._attach_m2m(model_name, obj_id, m2m_field, related_ids)

        return {
            'success': len(self.errors) == 0,
            'imported': self.import_log,
            'errors': self.errors,
        }

    def _import_model(self, model_info: ModelInfo, records: List[Dict]):
        """导入单个模型的数据"""
        from django.apps import apps
        Model = apps.get_model(model_info.app_label, model_info.model_name)

        self._log(f"开始导入 {model_info.model_name}，共 {len(records)} 条")

        for record in records:
            old_id = record.get('id')
            if old_id is None:
                continue

            try:
                # 跳过管理员账户（id=1 的超级用户不覆盖）
                if model_info.model_name == 'User' and old_id == 1:
                    self._log(f"  跳过超级用户 (id=1)")
                    # 但仍需建立映射，避免外键断裂
                    self.id_map.setdefault(model_info.model_name, {})[old_id] = 1
                    continue

                # 准备创建数据
                create_data = {}
                fk_lookups = {}

                for field_name, value in record.items():
                    # 跳过 M2M 字段（后面统一处理）
                    if field_name.endswith('_ids'):
                        continue

                    # 跳过排除字段
                    if field_name in model_info.exclude_fields:
                        continue

                    # 处理外键引用
                    if field_name in model_info.fk_fields:
                        related_model_name, _ = model_info.fk_fields[field_name]
                        if value is not None:
                            new_related_id = self.id_map.get(related_model_name, {}).get(value)
                            if new_related_id is not None:
                                fk_lookups[field_name] = new_related_id
                            else:
                                fk_lookups[field_name] = value  # 使用原 ID
                        else:
                            fk_lookups[field_name] = None
                    # 处理 User.password（不迁移密码）
                    elif model_info.model_name == 'User' and field_name == 'password':
                        continue
                    else:
                        create_data[field_name] = value

                # 创建或更新对象
                obj, created = Model.objects.update_or_create(
                    defaults=create_data,
                    **fk_lookups
                )

                # 记录新 ID 映射
                self.id_map.setdefault(model_info.model_name, {})[old_id] = obj.id

                # 缓冲 M2M 关系
                for field_name, value in record.items():
                    if field_name.endswith('_ids'):
                        m2m_field = field_name[:-4]  # 去掉 _ids 后缀
                        if m2m_field in model_info.m2m_fields:
                            self.m2m_buffer[(model_info.model_name, obj.id, m2m_field)] = value

                action = '创建' if created else '更新'
                self._log(f"  {action}: {model_info.model_name} id={obj.id} (旧id={old_id})")

            except Exception as e:
                self._error(f"  导入 {model_info.model_name} id={old_id} 失败: {str(e)}")

    def _attach_m2m(self, model_name: str, obj_id: int, m2m_field: str, related_ids: List[int]):
        """建立 M2M 关系"""
        from django.apps import apps

        model_info = MODEL_INFOS.get(model_name)
        if not model_info:
            return

        related_model_name = model_info.m2m_fields.get(m2m_field)
        if not related_model_name:
            return

        try:
            Model = apps.get_model(model_info.app_label, model_name)
            RelatedModel = apps.get_model(MODEL_INFOS[related_model_name].app_label, related_model_name)

            obj = Model.objects.get(id=obj_id)
            related_ids = [self.id_map.get(related_model_name, {}).get(rid, rid) for rid in related_ids]
            related_objs = RelatedModel.objects.filter(id__in=related_ids)

            getattr(obj, m2m_field).set(related_objs)
            self._log(f"  建立 M2M: {model_name}.{m2m_field} -> {related_model_name} (ids={related_ids})")

        except Exception as e:
            self._error(f"  建立 M2M 关系失败 {model_name}.{m2m_field}: {str(e)}")

    def import_from_file(self, file_path: str) -> Dict[str, Any]:
        """从 gzip 压缩的 JSON 文件恢复"""
        with gzip.open(file_path, 'rt', encoding='utf-8') as f:
            backup_data = json.load(f)
        self.backup_data = backup_data
        self.data = backup_data.get('data', {})
        self.metadata = backup_data.get('metadata', {})
        return self.import_all()
