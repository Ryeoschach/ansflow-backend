from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import ImageRegistryViewSet, ArtifactViewSet, ArtifactVersionViewSet

router = DefaultRouter()
router.register(r'registries', ImageRegistryViewSet, basename='registry')
router.register(r'artifacts', ArtifactViewSet, basename='artifact')
router.register(r'artifact-versions', ArtifactVersionViewSet, basename='artifact-version')

urlpatterns = [
    path('', include(router.urls)),
]
