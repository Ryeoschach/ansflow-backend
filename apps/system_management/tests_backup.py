import os
import gzip
import json
from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase
from django.contrib.auth import get_user_model
from apps.host_management.models import Host, Environment, SshCredential
from apps.pipeline_management.models import Pipeline
from apps.task_management.models import AnsibleTask, AnsibleSchedule
from django_celery_beat.models import PeriodicTask
from apps.registry_management.models import ImageRegistry, ArtifactoryInstance, ArtifactoryRepository, Artifact, ArtifactVersion
from apps.system_management.backup import BackupExporter, BackupImporter, MODULE_DEFINITIONS

class BackupModularTest(APITestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_superuser(username='backupadmin', password='password', email='backup@test.com')
        self.client.force_authenticate(user=self.user)
        
        # 创建一些跨模块数据
        self.env = Environment.objects.create(name="Backup Test Env", code="bt_env")
        self.host = Host.objects.create(hostname="backup-node", private_ip="10.9.9.9", env=self.env)
        self.pipeline = Pipeline.objects.create(name="Backup Test Pipeline", creator=self.user)

    def test_get_modules(self):
        """测试获取模块列表接口"""
        url = reverse('system-backup-modules')
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        keys = [m['key'] for m in response.data]
        self.assertIn('rbac', keys)
        self.assertIn('host', keys)
        self.assertIn('pipeline', keys)

    def test_modular_export(self):
        """测试按模块导出"""
        exporter = BackupExporter()
        
        # 仅导出 host 模块
        data = exporter.export(selected_modules=['host'])
        
        # 验证包含 Host 和 Environment
        self.assertIn('Host', data['data'])
        self.assertIn('Environment', data['data'])
        
        # 验证不包含 Pipeline (属于 pipeline 模块)
        self.assertNotIn('Pipeline', data['data'])

    def test_modular_restore(self):
        """测试按模块恢复"""
        # 1. 导出全部数据
        exporter = BackupExporter()
        full_data = exporter.export()
        
        # 2. 清理当前数据库数据 (模拟新环境)
        Pipeline.objects.all().delete()
        Host.objects.all().delete()
        Environment.objects.all().delete()
        
        # 3. 仅恢复 pipeline 模块
        importer = BackupImporter(full_data)
        result = importer.import_all(selected_modules=['pipeline'])
        
        self.assertTrue(result['success'])
        # 验证 Pipeline 已恢复
        self.assertTrue(Pipeline.objects.filter(name="Backup Test Pipeline").exists())
        # 验证 Host 未恢复
        self.assertFalse(Host.objects.filter(hostname="backup-node").exists())

    def test_passphrase_encryption_and_decryption(self):
        """测试使用密码加密导出与还原解密"""
        # 1. 创建包含敏感数据的 SshCredential
        cred = SshCredential.objects.create(
            name="Test Encrypted Cred",
            username="testuser",
            password="my-super-secret-password",
            private_key="my-private-key",
            passphrase="my-passphrase"
        )
        
        # 2. 导出备份（使用密码）
        passphrase = "SecretPassphrase@123"
        exporter = BackupExporter(passphrase=passphrase)
        backup_data = exporter.export(selected_modules=['host'])
        
        # 验证元数据中包含 has_encrypted_data 且为 True
        self.assertTrue(backup_data['metadata']['has_encrypted_data'])
        self.assertIn('encryption_salt', backup_data['metadata'])
        
        # 验证导出的数据已被加密 (不含有明文 "my-super-secret-password")
        exported_cred = None
        for record in backup_data['data'].get('SshCredential', []):
            if record['name'] == "Test Encrypted Cred":
                exported_cred = record
                break
        
        self.assertIsNotNone(exported_cred)
        self.assertNotEqual(exported_cred['password'], "my-super-secret-password")
        self.assertNotEqual(exported_cred['private_key'], "my-private-key")
        self.assertNotEqual(exported_cred['passphrase'], "my-passphrase")
        
        # 3. 删除凭据 (模拟新环境)
        cred.delete()
        self.assertFalse(SshCredential.objects.filter(name="Test Encrypted Cred").exists())
        
        # 4. 用错误的密码还原，验证报错解密失败且数据库回滚
        importer_wrong = BackupImporter(backup_data, passphrase="WrongPassphrase")
        result_wrong = importer_wrong.import_all(selected_modules=['host'])
        self.assertFalse(result_wrong['success'])
        self.assertTrue(any("解密敏感字段" in err or "Decryption failed" in err or "密码可能错误" in err for err in result_wrong['errors']))
        # 验证凭据依然不存在 (事务已回滚)
        self.assertFalse(SshCredential.objects.filter(name="Test Encrypted Cred").exists())
        
        # 5. 用正确的密码还原，验证成功解密且数据完整
        importer_correct = BackupImporter(backup_data, passphrase=passphrase)
        result_correct = importer_correct.import_all(selected_modules=['host'])
        self.assertTrue(result_correct['success'])
        
        restored_cred = SshCredential.objects.get(name="Test Encrypted Cred")
        self.assertEqual(restored_cred.password, "my-super-secret-password")
        self.assertEqual(restored_cred.private_key, "my-private-key")
        self.assertEqual(restored_cred.passphrase, "my-passphrase")

    def test_beat_task_sync_on_restore(self):
        """测试在备份还原后，AnsibleSchedule 能自动同步创建 PeriodicTask 周期任务"""
        # 1. 创建任务模板与定时调度
        task_tmpl = AnsibleTask.objects.create(
            name="Schedule Test Task",
            task_type="playbook",
            content="--- \n- hosts: all\n  tasks:\n    - ping:",
            creator=self.user
        )
        schedule = AnsibleSchedule.objects.create(
            name="Daily Test Run",
            task=task_tmpl,
            is_enabled=True,
            schedule_type="interval",
            interval_value=5,
            interval_unit="minutes",
            creator=self.user
        )
        
        # 2. 导出备份
        exporter = BackupExporter()
        backup_data = exporter.export(selected_modules=['task'])
        
        # 3. 清理数据库 (删除调度与模板，以及可能关联生成的 PeriodicTask)
        if schedule.periodic_task_id:
            PeriodicTask.objects.filter(id=schedule.periodic_task_id).delete()
        schedule.delete()
        task_tmpl.delete()
        
        self.assertFalse(AnsibleSchedule.objects.filter(name="Daily Test Run").exists())
        
        # 4. 还原备份
        importer = BackupImporter(backup_data)
        result = importer.import_all(selected_modules=['task'])
        
        self.assertTrue(result['success'])
        
        # 5. 验证 AnsibleSchedule 已恢复，并且关联的 PeriodicTask 也已生成
        restored_schedule = AnsibleSchedule.objects.get(name="Daily Test Run")
        self.assertIsNotNone(restored_schedule.periodic_task_id)
        
        # 验证 Celery Beat 的 PeriodicTask 真的在数据库中生成，并且是 enabled=True
        periodic_task = PeriodicTask.objects.get(id=restored_schedule.periodic_task_id)
        self.assertTrue(periodic_task.enabled)
        self.assertEqual(periodic_task.name, f"ansible_schedule_{restored_schedule.id}")

    def test_registry_backup_restore_with_passphrase(self):
        """测试使用密码加密备份与还原镜像/制品库配置"""
        # 1. 创建镜像与制品库数据
        registry = ImageRegistry.objects.create(
            name="Harbor-Test",
            url="https://harbor.test.com",
            username="admin",
            password="my-registry-password"
        )
        art_instance = ArtifactoryInstance.objects.create(
            name="JFrog-Test",
            url="https://jfrog.test.com/artifactory",
            username="admin",
            api_key="my-jfrog-api-key",
            password="my-jfrog-backup-password"
        )
        art_repo = ArtifactoryRepository.objects.create(
            instance=art_instance,
            repo_key="libs-release-local",
            repo_type="maven"
        )
        artifact = Artifact.objects.create(
            name="test-artifact",
            source_type="artifactory",
            type="jar",
            artifactory_repo=art_repo,
            pipeline=self.pipeline
        )
        artifact_ver = ArtifactVersion.objects.create(
            artifact=artifact,
            tag="v1.0.0"
        )

        # 2. 用密码导出备份
        passphrase = "RegistryPass@123"
        exporter = BackupExporter(passphrase=passphrase)
        backup_data = exporter.export(selected_modules=['registry', 'pipeline'])

        # 3. 清理数据库
        ArtifactVersion.objects.all().delete()
        Artifact.objects.all().delete()
        ArtifactoryRepository.objects.all().delete()
        ArtifactoryInstance.objects.all().delete()
        ImageRegistry.objects.all().delete()

        # 4. 用密码还原
        importer = BackupImporter(backup_data, passphrase=passphrase)
        result = importer.import_all(selected_modules=['registry', 'pipeline'])
        
        self.assertTrue(result['success'], f"Restore failed: {result.get('errors')}")

        # 5. 验证所有数据正确恢复
        self.assertTrue(ImageRegistry.objects.filter(name="Harbor-Test").exists())
        reg = ImageRegistry.objects.get(name="Harbor-Test")
        self.assertEqual(reg.password, "my-registry-password")

        self.assertTrue(ArtifactoryInstance.objects.filter(name="JFrog-Test").exists())
        inst = ArtifactoryInstance.objects.get(name="JFrog-Test")
        self.assertEqual(inst.api_key, "my-jfrog-api-key")
        self.assertEqual(inst.password, "my-jfrog-backup-password")

        self.assertTrue(ArtifactoryRepository.objects.filter(repo_key="libs-release-local").exists())
        
        self.assertTrue(Artifact.objects.filter(name="test-artifact").exists())
        self.assertTrue(ArtifactVersion.objects.filter(tag="v1.0.0").exists())

    def test_registry_backup_restore_without_passphrase(self):
        """测试不使用密码备份与还原镜像/制品库配置（在相同 SECRET_KEY 下应成功解密并还原）"""
        # 1. 创建镜像与制品库数据
        registry = ImageRegistry.objects.create(
            name="Harbor-NoPass",
            url="https://harbor.test.com",
            username="admin",
            password="my-registry-password"
        )
        art_instance = ArtifactoryInstance.objects.create(
            name="JFrog-NoPass",
            url="https://jfrog.test.com/artifactory",
            username="admin",
            api_key="my-jfrog-api-key",
            password="my-jfrog-backup-password"
        )

        # 2. 不用密码导出备份
        exporter = BackupExporter()
        backup_data = exporter.export(selected_modules=['registry'])

        # 3. 清理数据库
        ArtifactoryInstance.objects.all().delete()
        ImageRegistry.objects.all().delete()

        # 4. 不用密码还原
        importer = BackupImporter(backup_data)
        result = importer.import_all(selected_modules=['registry'])
        
        self.assertTrue(result['success'], f"Restore failed without passphrase: {result.get('errors')}")
        self.assertTrue(ImageRegistry.objects.filter(name="Harbor-NoPass").exists())
        self.assertTrue(ArtifactoryInstance.objects.filter(name="JFrog-NoPass").exists())
        
        reg = ImageRegistry.objects.get(name="Harbor-NoPass")
        inst = ArtifactoryInstance.objects.get(name="JFrog-NoPass")
        
        # 应当能成功还原
        self.assertEqual(reg.password, "my-registry-password")
        self.assertEqual(inst.api_key, "my-jfrog-api-key")
        self.assertEqual(inst.password, "my-jfrog-backup-password")

    def test_registry_backup_restore_different_secret_key(self):
        """测试跨环境（不同 SECRET_KEY）下，不使用密码备份的还原（应当优雅跳过解密失败的凭据字段）"""
        # 1. 创建镜像与制品库数据
        registry = ImageRegistry.objects.create(
            name="Harbor-DiffKey",
            url="https://harbor.test.com",
            username="admin",
            password="my-registry-password"
        )
        
        # 2. 不用密码导出备份（使用当前 SECRET_KEY）
        exporter = BackupExporter()
        backup_data = exporter.export(selected_modules=['registry'])
        
        # 3. 清理数据库
        ImageRegistry.objects.all().delete()
        
        # 4. 在不同的 SECRET_KEY 环境下恢复
        from django.test import override_settings
        with override_settings(SECRET_KEY="completely-different-secret-key-123"):
            importer = BackupImporter(backup_data)
            result = importer.import_all(selected_modules=['registry'])
            
            # 验证还原是否成功，但密码应该为空（因为无法解密）
            self.assertTrue(result['success'], f"Restore failed with different secret key: {result.get('errors')}")
            self.assertTrue(ImageRegistry.objects.filter(name="Harbor-DiffKey").exists())
            reg = ImageRegistry.objects.get(name="Harbor-DiffKey")
            self.assertEqual(reg.password, "")

    def test_registry_backup_restore_different_secret_key_existing(self):
        """测试跨环境（不同 SECRET_KEY）下，若数据库中已存在且包含密码，还原时不会将已有密码覆写为空"""
        # 1. 创建镜像数据
        registry = ImageRegistry.objects.create(
            name="Harbor-DiffKeyExist",
            url="https://harbor.test.com",
            username="admin",
            password="original-password"
        )
        
        # 2. 导出备份
        exporter = BackupExporter()
        backup_data = exporter.export(selected_modules=['registry'])
        
        # 3. 在数据库中修改密码（模拟现有环境里的正确配置）
        registry.password = "existing-correct-password"
        registry.save()
        
        # 4. 模拟在不同 SECRET_KEY 下恢复
        from django.test import override_settings
        with override_settings(SECRET_KEY="completely-different-secret-key-123"):
            importer = BackupImporter(backup_data)
            result = importer.import_all(selected_modules=['registry'])
            
            self.assertTrue(result['success'])
            
        reg = ImageRegistry.objects.get(name="Harbor-DiffKeyExist")
        # 密码应该保持为 existing-correct-password，不被覆写为 ""
        self.assertEqual(reg.password, "existing-correct-password")

    def test_config_item_backup_restore(self):
        """测试 ConfigItem 在有加密和非加密（多种数据类型）情况下的备份与恢复"""
        from apps.config_center.models import ConfigCategory, ConfigItem

        # 1. 创建分类与不同类型的配置项
        cat = ConfigCategory.objects.create(name="test_cat", label="测试分类")
        
        # 非加密配置项：布尔值
        ConfigItem.objects.create(
            category=cat,
            key="feishu.enabled",
            value=True,
            value_type="bool",
            is_encrypted=False
        )
        # 非加密配置项：列表
        ConfigItem.objects.create(
            category=cat,
            key="notify_on",
            value=["pipeline_start", "pipeline_result"],
            value_type="json",
            is_encrypted=False
        )
        # 加密配置项：字符串
        item_enc = ConfigItem(
            category=cat,
            key="secure_token",
            value_type="string",
            is_encrypted=True
        )
        item_enc.set_value("my-secret-token-value")
        item_enc.save()


        # 2. 导出备份
        exporter = BackupExporter()
        backup_data = exporter.export(selected_modules=['config'])

        # 3. 清理数据库
        ConfigItem.objects.all().delete()
        ConfigCategory.objects.all().delete()

        # 4. 还原备份
        importer = BackupImporter(backup_data)
        result = importer.import_all(selected_modules=['config'])
        self.assertTrue(result['success'], f"Restore failed: {result.get('errors')}")

        # 5. 验证是否还原成功
        self.assertTrue(ConfigCategory.objects.filter(name="test_cat").exists())
        
        restored_bool = ConfigItem.objects.get(category__name="test_cat", key="feishu.enabled")
        self.assertEqual(restored_bool.value, True)
        self.assertEqual(restored_bool.is_encrypted, False)

        restored_list = ConfigItem.objects.get(category__name="test_cat", key="notify_on")
        self.assertEqual(restored_list.value, ["pipeline_start", "pipeline_result"])
        self.assertEqual(restored_list.is_encrypted, False)

        restored_enc = ConfigItem.objects.get(category__name="test_cat", key="secure_token")
        self.assertEqual(restored_enc.is_encrypted, True)
        self.assertEqual(restored_enc.get_value(), "my-secret-token-value")

