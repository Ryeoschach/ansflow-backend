"""
系统备份与恢复模块

功能：
- 全量备份：导出所有业务数据为 JSON 文件
- 恢复导入：支持选择性恢复指定模块数据
- 加密字段：导出时跳过，导入时需手动录入（避免 SECRET_KEY 不一致导致解密失败）

导入采用三阶段策略：
1. Phase 1: 仅创建基础实例（忽略所有 FK 和 M2M）
2. Phase 2: 回填 FK 关系（直接操作 _id 字段）
3. Phase 3: 建立 M2M 关系

这样避免 FK 依赖问题和事务中断。
"""

import json
import gzip
import logging
import uuid
import os
import base64
from datetime import datetime, date
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass, field

from django.db import transaction
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

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
        'ConfigItem.value',
        'AIProvider.api_key',
        'HelmRepository.password',
    }


def is_encrypted_field(model_name: str, field_name: str) -> bool:
    return f'{model_name}.{field_name}' in get_encrypted_field_names()


def derive_key(passphrase: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=100000,
    )
    return kdf.derive(passphrase.encode())


def encrypt_data(plaintext: Any, key: bytes) -> str:
    if not isinstance(plaintext, str):
        plaintext = str(plaintext)
    aesgcm = AESGCM(key)
    nonce = os.urandom(12)
    ct = aesgcm.encrypt(nonce, plaintext.encode('utf-8'), None)
    return base64.b64encode(nonce + ct).decode('utf-8')



def decrypt_data(ciphertext_b64: str, key: bytes) -> str:
    aesgcm = AESGCM(key)
    combined = base64.b64decode(ciphertext_b64.encode('utf-8'))
    nonce = combined[:12]
    ct = combined[12:]
    pt = aesgcm.decrypt(nonce, ct, None)
    return pt.decode('utf-8')


def get_decrypted_field_value(obj, field_name: str) -> Optional[str]:
    """获取敏感/加密字段的明文值"""
    model_name = obj.__class__.__name__
    if model_name == 'AIProvider' and field_name == 'api_key':
        return obj.get_decrypted_key()
    elif model_name == 'ConfigItem' and field_name == 'value':
        return obj.get_value()
    else:
        val = getattr(obj, field_name)
        if val is None:
            return None
        return str(val)


# ============================================================
# 模块定义
# ============================================================

MODULE_DEFINITIONS = {
    'rbac': {
        'label': '权限与用户 (RBAC)',
        'models': ['Permission', 'Menu', 'Credential', 'Role', 'DataPolicy', 'User']
    },
    'host': {
        'label': '主机与资源池 (Hosts)',
        'models': ['SshCredential', 'Environment', 'Platform', 'Host', 'ResourcePool', 'HostBaseline']
    },
    'k8s': {
        'label': 'Kubernetes 管理 (K8s)',
        'models': ['K8sCluster', 'HelmRepository', 'K8sApplication']
    },
    'pipeline': {
        'label': '流水线 (Pipeline)',
        'models': ['Pipeline', 'PipelineVersion', 'PipelineWebhook', 'CIEnvironment', 'PipelineRun', 'PipelineNodeRun']
    },
    'registry': {
        'label': '镜像与制品 (Registry)',
        'models': ['ImageRegistry', 'ArtifactoryInstance', 'ArtifactoryRepository', 'Artifact', 'ArtifactVersion']
    },
    'task': {
        'label': '自动化任务 (Tasks)',
        'models': ['AnsibleTask', 'AnsibleExecution', 'AnsibleSchedule']
    },
    'config': {
        'label': '配置中心 (Config)',
        'models': ['ConfigCategory', 'ConfigItem']
    },
    'approval': {
        'label': '审批中心 (Approval)',
        'models': ['ApprovalPolicy', 'ApprovalTicket']
    },
    'ai': {
        'label': 'AI 引擎与知识库 (AI)',
        'models': ['KnowledgeBase', 'AIProvider', 'AIModel', 'AIConfig', 'KnowledgeDocument', 'KnowledgeChunk', 'AIPromptTemplate']
    },
    'sre': {
        'label': '自愈与自驱运维 (SRE)',
        'models': ['SelfHealingPolicy']
    },
}


# ============================================================
# 数据模型映射
# ============================================================

@dataclass
class ModelInfo:
    app_label: str
    model_name: str
    table_name: str
    pk_field: str = 'id'
    exclude_fields: List[str] = field(default_factory=list)
    fk_fields: Dict[str, Tuple[str, str]] = field(default_factory=dict)
    m2m_fields: Dict[str, str] = field(default_factory=dict)
    export_order: int = 99
    encrypted_fields: List[str] = field(default_factory=list)
    unique_fields: List[str] = field(default_factory=list)


MODEL_INFOS: Dict[str, ModelInfo] = {
    'Permission': ModelInfo(
        app_label='rbac_permission', model_name='Permission', table_name='rbac_permission',
        exclude_fields=['remark'],
        unique_fields=['code'],
        export_order=1,
    ),
    'Menu': ModelInfo(
        app_label='rbac_permission', model_name='Menu', table_name='rbac_permission_menu',
        fk_fields={'parent': ('Menu', 'id')},
        exclude_fields=['remark'],
        unique_fields=['key'],
        export_order=2,
    ),
    'Credential': ModelInfo(
        app_label='credentials_management', model_name='Credential', table_name='sys_credential_vault',
        exclude_fields=['remark'],
        encrypted_fields=['secret_value'],
        unique_fields=['name'],
        export_order=3,
    ),
    'SshCredential': ModelInfo(
        app_label='host_management', model_name='SshCredential', table_name='cmdb_ssh_credential',
        exclude_fields=['remark'],
        encrypted_fields=['password', 'private_key', 'passphrase'],
        unique_fields=['name'],
        export_order=4,
    ),
    'Environment': ModelInfo(
        app_label='host_management', model_name='Environment', table_name='cmdb_environment',
        exclude_fields=['remark'],
        unique_fields=['name'],
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
        unique_fields=['code'],
        export_order=6,
    ),
    'DataPolicy': ModelInfo(
        app_label='rbac_permission', model_name='DataPolicy', table_name='rbac_data_policy',
        fk_fields={'role': ('Role', 'id')},
        exclude_fields=['remark'],
        unique_fields=['role', 'resource_type', 'action_type'],
        export_order=7,
    ),
    'User': ModelInfo(
        app_label='rbac_permission', model_name='User', table_name='rbac_permission_user',
        m2m_fields={'roles': 'Role'},
        exclude_fields=['password', 'last_login', 'remark', 'date_joined',
                        'github_id', 'wechat_openid', 'ldap_dn', 'ldap_uid', 'login_type', 'avatar'],
        unique_fields=['username'],
        export_order=8,
    ),
    'Platform': ModelInfo(
        app_label='host_management', model_name='Platform', table_name='cmdb_platform',
        fk_fields={'default_credential': ('SshCredential', 'id')},
        exclude_fields=['remark'],
        encrypted_fields=['access_key', 'secret_key'],
        export_order=9,
    ),
    'K8sCluster': ModelInfo(
        app_label='k8s_management', model_name='K8sCluster', table_name='k8s_clusters',
        exclude_fields=['remark'],
        encrypted_fields=['kubeconfig_content', 'token'],
        unique_fields=['name'],
        export_order=10,
    ),
    'ImageRegistry': ModelInfo(
        app_label='registry_management', model_name='ImageRegistry', table_name='pipeline_image_registry',
        exclude_fields=['remark'],
        encrypted_fields=['password'],
        unique_fields=['name'],
        export_order=11,
    ),
    'ArtifactoryInstance': ModelInfo(
        app_label='registry_management', model_name='ArtifactoryInstance', table_name='registry_artifactory_instance',
        exclude_fields=['remark'],
        encrypted_fields=['api_key', 'password'],
        unique_fields=['name'],
        export_order=11.5,
    ),
    'ArtifactoryRepository': ModelInfo(
        app_label='registry_management', model_name='ArtifactoryRepository', table_name='registry_artifactory_repository',
        fk_fields={'instance': ('ArtifactoryInstance', 'id')},
        exclude_fields=['remark'],
        unique_fields=['instance', 'repo_key'],
        export_order=12,
    ),
    'Pipeline': ModelInfo(
        app_label='pipeline_management', model_name='Pipeline', table_name='pipeline_template',
        fk_fields={'creator': ('User', 'id')},
        exclude_fields=['remark'],
        unique_fields=['name'],
        export_order=13,
    ),
    'PipelineVersion': ModelInfo(
        app_label='pipeline_management', model_name='PipelineVersion', table_name='pipeline_version',
        fk_fields={
            'pipeline': ('Pipeline', 'id'),
            'creator': ('User', 'id'),
        },
        exclude_fields=['remark'],
        unique_fields=['pipeline', 'version_number'],
        export_order=13.5,
    ),
    'PipelineWebhook': ModelInfo(
        app_label='pipeline_management', model_name='PipelineWebhook', table_name='pipeline_webhook',
        fk_fields={'pipeline': ('Pipeline', 'id')},
        exclude_fields=['remark'],
        export_order=13.6,
    ),
    'PipelineRun': ModelInfo(
        app_label='pipeline_management', model_name='PipelineRun', table_name='pipeline_pipelinerun',
        fk_fields={
            'pipeline': ('Pipeline', 'id'),
            'trigger_user': ('User', 'id'),
            'parent_run': ('PipelineRun', 'id'),
        },
        exclude_fields=['remark'],
        unique_fields=['id'],  # 用 id 精确匹配
        export_order=14,
    ),
    'PipelineNodeRun': ModelInfo(
        app_label='pipeline_management', model_name='PipelineNodeRun', table_name='pipeline_node_run_log',
        fk_fields={
            'run': ('PipelineRun', 'id'),
            'approver': ('User', 'id'),
        },
        exclude_fields=['remark', 'logs'],
        unique_fields=['id'],
        export_order=14.5,
    ),
    'Artifact': ModelInfo(
        app_label='registry_management', model_name='Artifact', table_name='pipeline_artifact',
        fk_fields={
            'image_registry': ('ImageRegistry', 'id'),
            'artifactory_repo': ('ArtifactoryRepository', 'id'),
            'pipeline': ('Pipeline', 'id'),
        },
        exclude_fields=['remark'],
        export_order=15,
    ),
    'ArtifactVersion': ModelInfo(
        app_label='registry_management', model_name='ArtifactVersion', table_name='pipeline_artifact_version',
        fk_fields={
            'artifact': ('Artifact', 'id'),
            'pipeline_run': ('PipelineRun', 'id'),
        },
        exclude_fields=['remark'],
        export_order=16,
    ),
    'Host': ModelInfo(
        app_label='host_management', model_name='Host', table_name='cmdb_host',
        fk_fields={
            'env': ('Environment', 'id'),
            'platform': ('Platform', 'id'),
            'credential': ('SshCredential', 'id'),
        },
        exclude_fields=['remark'],
        unique_fields=['hostname'],
        export_order=17,
    ),
    'ResourcePool': ModelInfo(
        app_label='host_management', model_name='ResourcePool', table_name='cmdb_resource_pool',
        m2m_fields={'hosts': 'Host'},
        exclude_fields=['remark'],
        unique_fields=['code'],
        export_order=18,
    ),
    'CIEnvironment': ModelInfo(
        app_label='pipeline_management', model_name='CIEnvironment', table_name='pipeline_ci_environment',
        exclude_fields=['remark'],
        unique_fields=['name'],
        export_order=19,
    ),
    'AnsibleTask': ModelInfo(
        app_label='task_management', model_name='AnsibleTask', table_name='task_ansible_template',
        fk_fields={
            'resource_pool': ('ResourcePool', 'id'),
            'creator': ('User', 'id'),
        },
        exclude_fields=['remark'],
        unique_fields=['id'],
        export_order=19.5,
    ),
    'AnsibleExecution': ModelInfo(
        app_label='task_management', model_name='AnsibleExecution', table_name='task_ansible_execution',
        fk_fields={
            'task': ('AnsibleTask', 'id'),
            'executor': ('User', 'id'),
        },
        exclude_fields=['remark'],
        unique_fields=['id'],
        export_order=19.6,
    ),
    'AnsibleSchedule': ModelInfo(
        app_label='task_management', model_name='AnsibleSchedule', table_name='task_ansible_schedule',
        fk_fields={
            'task': ('AnsibleTask', 'id'),
            'creator': ('User', 'id'),
        },
        exclude_fields=['remark'],
        unique_fields=['id'],
        export_order=19.7,
    ),
    'ConfigCategory': ModelInfo(
        app_label='config_center', model_name='ConfigCategory', table_name='config_center_category',
        exclude_fields=['remark'],
        unique_fields=['name'],
        export_order=20,
    ),
    'ConfigItem': ModelInfo(
        app_label='config_center', model_name='ConfigItem', table_name='config_center_item',
        fk_fields={'category': ('ConfigCategory', 'id')},
        exclude_fields=['remark'],
        encrypted_fields=['value'],
        unique_fields=['category', 'key'],
        export_order=21,
    ),
    'ApprovalPolicy': ModelInfo(
        app_label='approval_center', model_name='ApprovalPolicy', table_name='approval_policy',
        fk_fields={},
        m2m_fields={'approver_roles': 'Role'},
        exclude_fields=['remark'],
        export_order=22,
    ),
    'ApprovalTicket': ModelInfo(
        app_label='approval_center', model_name='ApprovalTicket', table_name='approval_ticket',
        fk_fields={
            'submitter': ('User', 'id'),
            'approver': ('User', 'id'),
        },
        exclude_fields=['remark'],
        unique_fields=['id'],
        export_order=23,
    ),
    'KnowledgeBase': ModelInfo(
        app_label='ai_engine', model_name='KnowledgeBase', table_name='ai_knowledge_base',
        exclude_fields=['remark'],
        unique_fields=['name'],
        export_order=23.5,
    ),
    'AIProvider': ModelInfo(
        app_label='ai_engine', model_name='AIProvider', table_name='ai_provider',
        exclude_fields=['remark'],
        encrypted_fields=['api_key'],
        unique_fields=['name'],
        export_order=24,
    ),
    'AIModel': ModelInfo(
        app_label='ai_engine', model_name='AIModel', table_name='ai_model',
        fk_fields={'provider': ('AIProvider', 'id')},
        exclude_fields=['remark'],
        unique_fields=['provider', 'name'],
        export_order=25,
    ),
    'AIConfig': ModelInfo(
        app_label='ai_engine', model_name='AIConfig', table_name='ai_config',
        fk_fields={
            'default_llm': ('AIModel', 'id'),
            'default_embedding': ('AIModel', 'id'),
            'default_vision': ('AIModel', 'id'),
            'default_rerank': ('AIModel', 'id'),
            'default_kb': ('KnowledgeBase', 'id'),
        },
        exclude_fields=['remark'],
        unique_fields=['name'],
        export_order=26,
    ),
    'AIPromptTemplate': ModelInfo(
        app_label='ai_engine', model_name='AIPromptTemplate', table_name='ai_prompt_template',
        exclude_fields=['remark'],
        unique_fields=['code'],
        export_order=27,
    ),
    'HelmRepository': ModelInfo(
        app_label='k8s_management', model_name='HelmRepository', table_name='helm_repositories',
        encrypted_fields=['password'],
        unique_fields=['name'],
        export_order=10.2,
    ),
    'K8sApplication': ModelInfo(
        app_label='k8s_management', model_name='K8sApplication', table_name='k8s_applications',
        fk_fields={'cluster': ('K8sCluster', 'id')},
        exclude_fields=['error_message', 'diff_details'],
        unique_fields=['name'],
        export_order=10.5,
    ),
    'HostBaseline': ModelInfo(
        app_label='host_management', model_name='HostBaseline', table_name='cmdb_host_baseline',
        fk_fields={'resource_pool': ('ResourcePool', 'id')},
        export_order=18.5,
    ),
    'SelfHealingPolicy': ModelInfo(
        app_label='sre_management', model_name='SelfHealingPolicy', table_name='sre_healing_policy',
        fk_fields={'pipeline': ('Pipeline', 'id')},
        unique_fields=['name'],
        export_order=23.4,
    ),
    'KnowledgeDocument': ModelInfo(
        app_label='ai_engine', model_name='KnowledgeDocument', table_name='ai_knowledge_document',
        fk_fields={'kb': ('KnowledgeBase', 'id')},
        export_order=23.6,
    ),
    'KnowledgeChunk': ModelInfo(
        app_label='ai_engine', model_name='KnowledgeChunk', table_name='ai_knowledge_chunk',
        fk_fields={'document': ('KnowledgeDocument', 'id')},
        export_order=23.7,
    ),
}


# ============================================================
# 备份导出器
# ============================================================

class BackupExporter:
    VERSION = '1.0'

    def __init__(self, passphrase: Optional[str] = None):
        self.passphrase = passphrase
        self.data: Dict[str, List[Dict]] = {}
        self.metadata: Dict[str, Any] = {
            'version': self.VERSION,
            'created_at': datetime.now().isoformat(),
            'encrypted_fields': list(get_encrypted_field_names()),
            'has_encrypted_data': False,
        }
        if self.passphrase:
            salt_bytes = os.urandom(16)
            self.metadata['encryption_salt'] = base64.b64encode(salt_bytes).decode('utf-8')
            self.metadata['has_encrypted_data'] = True
            self.key = derive_key(self.passphrase, salt_bytes)
        else:
            from django.conf import settings
            salt_bytes = os.urandom(16)
            self.metadata['encryption_salt'] = base64.b64encode(salt_bytes).decode('utf-8')
            self.metadata['has_encrypted_data'] = True
            self.metadata['encrypted_by_secret_key'] = True
            self.key = derive_key(settings.SECRET_KEY, salt_bytes)

    def export(self, selected_modules: Optional[List[str]] = None) -> Dict[str, Any]:
        """执行导出
        :param selected_modules: 指定要导出的模块列表（key），None 表示导出全部
        """
        from django.apps import apps

        # 1. 确定要导出的模型范围
        target_model_names = []
        if selected_modules:
            for mod_key in selected_modules:
                if mod_key in MODULE_DEFINITIONS:
                    target_model_names.extend(MODULE_DEFINITIONS[mod_key]['models'])
        else:
            target_model_names = list(MODEL_INFOS.keys())

        # 2. 过滤并按顺序排序
        sorted_models = []
        for name in target_model_names:
            if name in MODEL_INFOS:
                sorted_models.append((name, MODEL_INFOS[name]))
        sorted_models.sort(key=lambda x: x[1].export_order)

        # 3. 执行导出
        for model_name, model_info in sorted_models:
            Model = apps.get_model(model_info.app_label, model_name)
            records = []

            # 基础排除字段
            base_excludes = {'create_time', 'update_time'} | set(model_info.exclude_fields)

            for obj in Model.objects.all():
                record = {'id': obj.id}

                # 处理普通字段和外键
                for field in obj._meta.fields:
                    if field.name in base_excludes or field.name == 'id':
                        continue

                    # 如果是敏感/加密字段，进行加密
                    is_field_encrypted = field.name in model_info.encrypted_fields
                    if is_field_encrypted and model_name == 'ConfigItem' and field.name == 'value':
                        is_field_encrypted = obj.is_encrypted

                    if is_field_encrypted and self.key:
                        plaintext = get_decrypted_field_value(obj, field.name)
                        if plaintext is not None:
                            record[field.name] = encrypt_data(plaintext, self.key)
                        else:
                            record[field.name] = None
                        continue


                    value = getattr(obj, field.name)
                    # 处理外键
                    if field.is_relation and value:
                        record[field.name] = value.pk
                    # 处理日期时间
                    elif isinstance(value, (datetime, date)):
                        record[field.name] = value.isoformat()
                    # 处理 UUID
                    elif isinstance(value, uuid.UUID):
                        record[field.name] = str(value)
                    else:
                        record[field.name] = value

                # 处理 M2M
                for m2m_field in model_info.m2m_fields:
                    record[f"{m2m_field}_ids"] = list(
                        getattr(obj, m2m_field).values_list('pk', flat=True)
                    )

                records.append(record)

            if records:
                self.data[model_name] = records
                logger.info(f"[Backup] 导出 {model_name}: {len(records)} 条")

        return {
            'metadata': self.metadata,
            'data': self.data,
        }

    def export_to_file(self, file_path: str):
        """导出为 gzip 压缩的 JSON 文件"""
        with gzip.open(file_path, 'wt', encoding='utf-8') as f:
            json.dump(self.export(), f, ensure_ascii=False, indent=2)


# ============================================================
# 备份恢复导入器 (三阶段)
# ============================================================

class DecryptionError(Exception):
    pass


class BackupImporter:
    def __init__(self, backup_data: Dict[str, Any], passphrase: Optional[str] = None):
        self.data = backup_data.get('data', {})
        self.metadata = backup_data.get('metadata', {})
        self.passphrase = passphrase
        self.id_map: Dict[str, Dict[int, int]] = {}
        self.errors: List[str] = []
        self.imported_counts: Dict[str, int] = {}
        self.key = None

    def _derive_key_if_needed(self):
        # Derive key if passphrase and encrypted data exist
        has_encrypted_data = self.metadata.get('has_encrypted_data', False)
        salt_b64 = self.metadata.get('encryption_salt', '')
        if has_encrypted_data and salt_b64:
            if self.passphrase:
                salt_bytes = base64.b64decode(salt_b64.encode('utf-8'))
                self.key = derive_key(self.passphrase, salt_bytes)
            elif self.metadata.get('encrypted_by_secret_key', False):
                from django.conf import settings
                salt_bytes = base64.b64decode(salt_b64.encode('utf-8'))
                self.key = derive_key(settings.SECRET_KEY, salt_bytes)

    def _log(self, msg: str):
        logger.info(f"[Restore] {msg}")

    def _error(self, msg: str):
        self.errors.append(msg)
        logger.error(f"[Restore] 错误: {msg}")

    def import_all(self, selected_modules: Optional[List[str]] = None) -> Dict[str, Any]:
        """执行恢复导入
        :param selected_modules: 指定要恢复的模块列表（key），None 表示恢复全部
        """
        self._derive_key_if_needed()
        from django.apps import apps

        # 1. 确定要恢复的模型范围
        target_model_names = []
        if selected_modules:
            for mod_key in selected_modules:
                if mod_key in MODULE_DEFINITIONS:
                    target_model_names.extend(MODULE_DEFINITIONS[mod_key]['models'])
        else:
            # 如果不指定，则恢复备份文件中包含的所有模型
            target_model_names = list(self.data.keys())

        # 2. 过滤并按顺序排序
        sorted_models = []
        for name in target_model_names:
            if name in MODEL_INFOS:
                sorted_models.append((name, MODEL_INFOS[name]))
        sorted_models.sort(key=lambda x: x[1].export_order)

        try:
            with transaction.atomic():
                # Phase 1: 创建基础实例
                self._log(f"=== 阶段 1: 创建基础实例 (模块: {selected_modules or 'ALL'}) ===")
                for model_name, model_info in sorted_models:
                    records = self.data.get(model_name, [])
                    if records:
                        self._import_phase_1(model_name, model_info, records)

                # Phase 2: 回填 FK 关系
                self._log("=== 阶段 2: 回填 FK 关系 ===")
                for model_name, model_info in sorted_models:
                    records = self.data.get(model_name, [])
                    if records:
                        self._import_phase_2(model_name, model_info, records)

                # Phase 3: 建立 M2M 关系
                self._log("=== 阶段 3: 建立 M2M 关系 ===")
                for model_name, model_info in sorted_models:
                    records = self.data.get(model_name, [])
                    if records:
                        self._import_phase_3(model_name, model_info, records)

            # Celery Beat 定时任务自动同步机制
            if 'AnsibleSchedule' in self.id_map:
                try:
                    from apps.task_management.tasks import sync_schedule_to_beat
                    from apps.task_management.models import AnsibleSchedule
                    for old_id, new_id in self.id_map['AnsibleSchedule'].items():
                        try:
                            schedule = AnsibleSchedule.objects.get(id=new_id)
                            sync_schedule_to_beat(schedule)
                            self._log(f"Synced schedule {schedule.id} to Celery Beat")
                        except Exception as ex:
                            self._error(f"Failed to sync schedule {new_id} to Celery Beat: {ex}")
                except Exception as ex:
                    self._error(f"Failed to import or sync schedules to beat: {ex}")

        except Exception as e:
            self._error(f"恢复过程中发生严重错误: {str(e)}")

        return {
            'success': len(self.errors) == 0,
            'errors': self.errors,
            'imported': self.imported_counts,
        }

    def _import_phase_1(self, model_name: str, model_info: ModelInfo, records: List[Dict]):
        """阶段 1: 仅创建基础实例，忽略 FK 和 M2M (但为了满足 NOT NULL 约束，尝试提前填充已存在的 FK)"""
        from django.apps import apps
        Model = apps.get_model(model_info.app_label, model_info.model_name)

        self._log(f"Phase 1: {model_name} ({len(records)} 条)")

        for record in records:
            old_id = record.get('id')
            if old_id is None:
                continue

            # 跳过超级用户
            if model_name == 'User' and old_id == 1:
                self.id_map.setdefault(model_name, {})[old_id] = 1
                self._log(f"  跳过超级用户 (id=1)")
                continue

            try:
                # 构建仅包含非 FK/M2M 字段的数据
                clean_data = {}
                for field_name, value in record.items():
                    if field_name.endswith('_ids'):
                        continue  # 跳过 M2M

                    if field_name in model_info.fk_fields:
                        # 尝试提前填充 FK，以满足 NOT NULL 约束
                        rel_model, _ = model_info.fk_fields[field_name]
                        new_fk_id = self.id_map.get(rel_model, {}).get(value)
                        if new_fk_id:
                            clean_data[field_name + '_id'] = new_fk_id
                        continue

                    if field_name in model_info.exclude_fields:
                        continue

                    is_field_encrypted = field_name in model_info.encrypted_fields
                    if is_field_encrypted and model_name == 'ConfigItem' and field_name == 'value':
                        is_field_encrypted = record.get('is_encrypted', False)

                    if is_field_encrypted:
                        if self.key and value is not None:
                            try:
                                decrypted_val = decrypt_data(value, self.key)
                                
                                # ConfigItem 特殊处理
                                if model_name == 'ConfigItem' and field_name == 'value':
                                    if record.get('is_encrypted'):
                                        from utils.encryption import encrypt_string
                                        clean_data[field_name] = encrypt_string(decrypted_val)
                                    else:
                                        clean_data[field_name] = decrypted_val
                                else:
                                    clean_data[field_name] = decrypted_val
                            except Exception as e:
                                if self.metadata.get('encrypted_by_secret_key', False):
                                    self._log(f"警告: 使用本地 SECRET_KEY 解密敏感字段 {model_name}.{field_name} 失败，可能是跨环境还原。跳过该字段还原。")
                                else:
                                    raise DecryptionError(f"解密敏感字段 {model_name}.{field_name} 失败，密码可能错误: {str(e)}")
                        continue

                    if field_name == 'id':
                        continue
                    if model_name == 'User' and field_name == 'password':
                        continue
                    clean_data[field_name] = value

                # 查找已存在的记录
                obj = None
                if model_info.unique_fields:
                    lookup = {}
                    for k in model_info.unique_fields:
                        if k in record:
                            lookup[k] = record[k]
                    if lookup:
                        obj = Model.objects.filter(**lookup).first()

                # 仅在需要创建新记录时，动态填充缺失的且不可为空（NOT NULL）且没有定义默认值的字段，防止在严格数据库（如 Postgres/MySQL）中报错
                if not obj:
                    from django.db import models as django_models
                    for field in Model._meta.fields:
                        if field.name == 'id' or field.primary_key:
                            continue
                        if field.name in model_info.m2m_fields:
                            continue
                        if field.is_relation and field.many_to_one:
                            continue
                        if getattr(field, 'auto_now', False) or getattr(field, 'auto_now_add', False):
                            continue

                        if not field.null and field.default == django_models.fields.NOT_PROVIDED:
                            if field.name not in clean_data:
                                if isinstance(field, (django_models.IntegerField, django_models.FloatField, django_models.DecimalField, django_models.AutoField)):
                                    clean_data[field.name] = 0
                                elif isinstance(field, django_models.BooleanField):
                                    clean_data[field.name] = False
                                elif isinstance(field, (django_models.DateTimeField, django_models.DateField, django_models.TimeField)):
                                    from django.utils import timezone
                                    if isinstance(field, django_models.DateTimeField):
                                        clean_data[field.name] = timezone.now()
                                    elif isinstance(field, django_models.DateField):
                                        clean_data[field.name] = timezone.now().date()
                                    else:
                                        clean_data[field.name] = timezone.now().time()
                                elif isinstance(field, (django_models.CharField, django_models.TextField)):
                                    clean_data[field.name] = ""
                                elif isinstance(field, django_models.JSONField):
                                    clean_data[field.name] = {}
                                else:
                                    clean_data[field.name] = ""

                if obj:
                    # 更新
                    for k, v in clean_data.items():
                        setattr(obj, k, v)
                    if clean_data:
                        fields_to_update = list(clean_data.keys())
                        if hasattr(obj, 'update_time'):
                            fields_to_update.append('update_time')
                        obj.save(update_fields=fields_to_update)
                    self._log(f"  更新: {model_name} id={obj.id} (旧id={old_id})")
                else:
                    # 创建（可能因 UNIQUE 约束失败，改用 get_or_create）
                    try:
                        with transaction.atomic():
                            obj = Model.objects.create(**clean_data)
                            self._log(f"  创建: {model_name} id={obj.id} (旧id={old_id})")
                    except Exception:
                        # UNIQUE 约束失败时，改用 get_or_create（确保记录存在即可，不重复创建）
                        lookup = {k: record[k] for k in model_info.unique_fields if k in record}
                        if lookup:
                            obj, _ = Model.objects.get_or_create(defaults=clean_data, **lookup)
                            self._log(f"  创建(get_or_create): {model_name} id={obj.id} (旧id={old_id})")
                        else:
                            raise

                self.id_map.setdefault(model_name, {})[old_id] = obj.id
                self.imported_counts[model_name] = self.imported_counts.get(model_name, 0) + 1

            except DecryptionError:
                raise
            except Exception as e:
                self._error(f"  {model_name}[{old_id}] Phase1 失败: {str(e)}")

    def _import_phase_2(self, model_name: str, model_info: ModelInfo, records: List[Dict]):
        """阶段 2: 回填 FK 关系"""
        if not model_info.fk_fields:
            return

        from django.apps import apps
        Model = apps.get_model(model_info.app_label, model_info.model_name)

        self._log(f"Phase 2: {model_name} ({len(records)} 条)")

        for record in records:
            old_id = record.get('id')
            new_id = self.id_map.get(model_name, {}).get(old_id)
            if not new_id:
                continue

            try:
                update_data = {}
                for field_name, (rel_model, _) in model_info.fk_fields.items():
                    old_fk_id = record.get(field_name)
                    if old_fk_id is None:
                        continue

                    new_fk_id = self.id_map.get(rel_model, {}).get(old_fk_id)
                    if new_fk_id is not None:
                        update_data[field_name + '_id'] = new_fk_id
                    else:
                        self._log(f"  {model_name}[{old_id}] {field_name}: 旧 id={old_fk_id} 无法映射")

                if update_data:
                    Model.objects.filter(pk=new_id).update(**update_data)
                    self._log(f"  回填 FK: {model_name} id={new_id}")

            except Exception as e:
                self._error(f"  {model_name}[{old_id}] Phase2 失败: {str(e)}")

    def _import_phase_3(self, model_name: str, model_info: ModelInfo, records: List[Dict]):
        """阶段 3: 建立 M2M 关系"""
        if not model_info.m2m_fields:
            return

        from django.apps import apps
        Model = apps.get_model(model_info.app_label, model_name)

        self._log(f"Phase 3: {model_name} ({len(records)} 条)")

        for record in records:
            old_id = record.get('id')
            new_id = self.id_map.get(model_name, {}).get(old_id)
            if not new_id:
                continue

            try:
                obj = Model.objects.get(pk=new_id)
                for field_name, rel_model_name in model_info.m2m_fields.items():
                    old_m2m_ids = record.get(f"{field_name}_ids", [])
                    new_m2m_ids = []
                    for oid in old_m2m_ids:
                        mapped = self.id_map.get(rel_model_name, {}).get(oid)
                        if mapped:
                            new_m2m_ids.append(mapped)

                    if new_m2m_ids:
                        getattr(obj, field_name).set(new_m2m_ids)
                        self._log(f"  M2M: {model_name}.{field_name} -> {rel_model_name} ({len(new_m2m_ids)} 条)")

            except Exception as e:
                self._error(f"  {model_name}[{old_id}] Phase3 失败: {str(e)}")

    def import_from_file(self, file_path: str, selected_modules: Optional[List[str]] = None) -> Dict[str, Any]:
        """从 gzip 压缩的 JSON 文件恢复"""
        with gzip.open(file_path, 'rt', encoding='utf-8') as f:
            backup_data = json.load(f)
        self.data = backup_data.get('data', {})
        self.metadata = backup_data.get('metadata', {})
        return self.import_all(selected_modules=selected_modules)