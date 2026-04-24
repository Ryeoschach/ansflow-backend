from django.urls import path, include
from rest_framework.routers import DefaultRouter
from apps.task_management.views import AnsibleTaskViewSet, AnsibleExecutionViewSet, AnsibleScheduleViewSet

router = DefaultRouter()
router.register(r'ansible_tasks', AnsibleTaskViewSet)
router.register(r'executions', AnsibleExecutionViewSet)
router.register(r'schedules', AnsibleScheduleViewSet)

urlpatterns = [
    path('', include(router.urls)),
]
