from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    ImageRegistryViewSet,
    ArtifactoryInstanceViewSet,
    ArtifactoryRepositoryViewSet,
    ArtifactViewSet,
    ArtifactVersionViewSet,
)

router = DefaultRouter()
router.register(r'registries', ImageRegistryViewSet, basename='registry')
router.register(r'artifacts', ArtifactViewSet, basename='artifact')
router.register(r'artifact-versions', ArtifactVersionViewSet, basename='artifact-version')
router.register(r'artifactory/instances', ArtifactoryInstanceViewSet, basename='artifactory-instance')
router.register(r'artifactory/repositories', ArtifactoryRepositoryViewSet, basename='artifactory-repo')

urlpatterns = [
    path('', include(router.urls)),
]
