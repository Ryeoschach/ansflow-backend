from rest_framework.routers import DefaultRouter
from .views import ConfigCategoryViewSet, ConfigItemViewSet

router = DefaultRouter()
router.register(r'config/categories', ConfigCategoryViewSet, basename='config-categories')
router.register(r'config/items', ConfigItemViewSet, basename='config-items')

urlpatterns = router.urls
