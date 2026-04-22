from rest_framework import serializers
from .models import ImageRegistry, ArtifactoryInstance, ArtifactoryRepository, Artifact, ArtifactVersion


class ImageRegistrySerializer(serializers.ModelSerializer):
    class Meta:
        model = ImageRegistry
        fields = '__all__'
        extra_kwargs = {
            'password': {'write_only': True}
        }


class ArtifactoryInstanceSerializer(serializers.ModelSerializer):
    class Meta:
        model = ArtifactoryInstance
        fields = '__all__'
        extra_kwargs = {
            'api_key': {'write_only': True},
            'password': {'write_only': True},
        }


class ArtifactoryRepositorySerializer(serializers.ModelSerializer):
    instance_name = serializers.CharField(source='instance.name', read_only=True)
    instance_url = serializers.CharField(source='instance.url', read_only=True)

    class Meta:
        model = ArtifactoryRepository
        fields = [
            'id', 'instance', 'instance_name', 'instance_url',
            'repo_key', 'repo_type', 'description', 'is_active',
            'create_time', 'update_time'
        ]
        read_only_fields = ['id', 'create_time', 'update_time']


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
    registry_name = serializers.CharField(source='image_registry.name', read_only=True)
    artifactory_repo_name = serializers.CharField(source='artifactory_repo.repo_key', read_only=True)
    pipeline_name = serializers.CharField(source='pipeline.name', read_only=True)
    version_count = serializers.SerializerMethodField()
    versions = ArtifactVersionSerializer(many=True, read_only=True)

    class Meta:
        model = Artifact
        fields = [
            'id', 'name', 'source_type', 'type',
            'image_registry', 'registry_name',
            'artifactory_repo', 'artifactory_repo_name',
            'repository', 'latest_tag', 'latest_digest', 'latest_size',
            'description', 'pipeline', 'pipeline_name',
            'version_count', 'versions',
            'create_time', 'update_time'
        ]
        read_only_fields = ['id', 'create_time', 'update_time']

    def get_version_count(self, obj):
        return obj.versions.count()


class ArtifactDetailSerializer(ArtifactSerializer):
    versions = ArtifactVersionSerializer(many=True, read_only=True)

    class Meta(ArtifactSerializer.Meta):
        fields = ArtifactSerializer.Meta.fields
