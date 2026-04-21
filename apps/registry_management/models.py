from django.db import models
from utils.base_model import BaseModel
from utils.fields import EncryptedCharField


class ImageRegistry(BaseModel):
    """
    统一镜像仓库管理
    """
    name = models.CharField(max_length=100, unique=True, verbose_name="仓库名称", help_text="如: Harbor-ProjA")
    url = models.CharField(max_length=255, verbose_name="仓库地址", help_text="如: https://hub.docker.com 或 harbor.domain.com")
    namespace = models.CharField(max_length=100, blank=True, null=True, verbose_name="默认命名空间", help_text="如: library (可选)")
    username = models.CharField(max_length=100, verbose_name="认证用户名")
    password = EncryptedCharField(max_length=255, verbose_name="认证密码/Token")
    description = models.TextField(blank=True, null=True, verbose_name="描述信息")

    class Meta:
        db_table = 'pipeline_image_registry'
        verbose_name = "镜像仓库"
        verbose_name_plural = verbose_name
        ordering = ['-create_time']

    def __str__(self):
        return self.name


class Artifact(BaseModel):
    """
    流水线产物（Artifact）记录
    """
    TYPE_CHOICES = [
        ('docker_image', 'Docker 镜像'),
        ('jar', 'JAR 包'),
        ('binary', '二进制文件'),
        ('helm_chart', 'Helm Chart'),
        ('other', '其他'),
    ]

    name = models.CharField(max_length=255, verbose_name="产物名称", help_text="如: backend-api, frontend-web")
    type = models.CharField(max_length=20, choices=TYPE_CHOICES, default='docker_image', verbose_name="产物类型")
    registry = models.ForeignKey(ImageRegistry, on_delete=models.SET_NULL, null=True, blank=True, related_name='artifacts', verbose_name="所属仓库")
    repository = models.CharField(max_length=255, blank=True, null=True, verbose_name="仓库路径", help_text="如: library/backend-api")
    latest_tag = models.CharField(max_length=128, blank=True, null=True, verbose_name="最新版本")
    latest_digest = models.CharField(max_length=128, blank=True, null=True, verbose_name="最新 Digest (SHA256)")
    latest_size = models.BigIntegerField(default=0, verbose_name="最新大小 (bytes)")
    description = models.TextField(blank=True, null=True, verbose_name="描述")
    pipeline = models.ForeignKey('pipeline_management.Pipeline', on_delete=models.SET_NULL, null=True, blank=True, related_name='artifacts', verbose_name="关联流水线")

    class Meta:
        db_table = 'pipeline_artifact'
        verbose_name = "流水线产物"
        verbose_name_plural = verbose_name
        ordering = ['-create_time']
        indexes = [
            models.Index(fields=['name']),
            models.Index(fields=['type']),
        ]

    def __str__(self):
        return f"{self.name}:{self.latest_tag}"


class ArtifactVersion(BaseModel):
    """
    产物版本记录
    """
    artifact = models.ForeignKey(Artifact, on_delete=models.CASCADE, related_name='versions', verbose_name="所属产物")
    tag = models.CharField(max_length=128, verbose_name="版本标签", help_text="如: v1.0.0, latest, 20240101")
    digest = models.CharField(max_length=128, blank=True, null=True, verbose_name="Digest (SHA256)")
    size = models.BigIntegerField(default=0, verbose_name="大小 (bytes)")
    image_url = models.CharField(max_length=512, blank=True, null=True, verbose_name="完整镜像地址")
    build_user = models.CharField(max_length=100, blank=True, null=True, verbose_name="构建人")
    commit_sha = models.CharField(max_length=64, blank=True, null=True, verbose_name="代码 Commit SHA")
    pipeline_run = models.ForeignKey('pipeline_management.PipelineRun', on_delete=models.SET_NULL, null=True, blank=True, related_name='artifact_versions', verbose_name="关联流水线运行")
    metadata = models.JSONField(default=dict, blank=True, verbose_name="元数据")

    class Meta:
        db_table = 'pipeline_artifact_version'
        verbose_name = "产物版本"
        verbose_name_plural = verbose_name
        ordering = ['-create_time']
        unique_together = [['artifact', 'tag']]
        indexes = [
            models.Index(fields=['artifact', 'tag']),
            models.Index(fields=['digest']),
        ]

    def __str__(self):
        return f"{self.artifact.name}:{self.tag}"
