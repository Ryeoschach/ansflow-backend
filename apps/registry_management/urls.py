from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import ImageRegistryViewSet

router = DefaultRouter()
router.register(r'registries', ImageRegistryViewSet, basename='registry')

urlpatterns = [
    path('', include(router.urls)),
]
