from rest_framework import serializers
from .models import ImageRegistry, Artifact, ArtifactVersion


class ImageRegistrySerializer(serializers.ModelSerializer):
    class Meta:
        model = ImageRegistry
        fields = '__all__'
        extra_kwargs = {
            'password': {'write_only': True} # 密码只写，不返回给前端
        }


class ArtifactVersionSerializer(serializers.ModelSerializer):
    artifact_name = serializers.CharField(source='artifact.name', read_only=True)

    class Meta:
        model = ArtifactVersion
        fields = [
            'id', 'artifact', 'artifact_name', 'tag', 'digest', 'size',
            'image_url', 'build_user', 'commit_sha', 'pipeline_run',
            'metadata', 'create_time', 'update_time'
        ]
        read_only_fields = ['id', 'create_time', 'update_time']


class ArtifactSerializer(serializers.ModelSerializer):
    registry_name = serializers.CharField(source='registry.name', read_only=True)
    pipeline_name = serializers.CharField(source='pipeline.name', read_only=True)
    version_count = serializers.SerializerMethodField()
    versions = ArtifactVersionSerializer(many=True, read_only=True)

    class Meta:
        model = Artifact
        fields = [
            'id', 'name', 'type', 'registry', 'registry_name', 'repository',
            'latest_tag', 'latest_digest', 'latest_size', 'description',
            'pipeline', 'pipeline_name', 'version_count', 'versions',
            'create_time', 'update_time'
        ]
        read_only_fields = ['id', 'create_time', 'update_time']

    def get_version_count(self, obj):
        return obj.versions.count()


class ArtifactDetailSerializer(ArtifactSerializer):
    versions = ArtifactVersionSerializer(many=True, read_only=True)

    class Meta(ArtifactSerializer.Meta):
        fields = ArtifactSerializer.Meta.fields
