from django.urls import path, include
from rest_framework.routers import DefaultRouter
from apps.task_management.views import AnsibleTaskViewSet

router = DefaultRouter()
router.register(r'ansible_tasks', AnsibleTaskViewSet)

urlpatterns = [
    path('', include(router.urls)),
]
