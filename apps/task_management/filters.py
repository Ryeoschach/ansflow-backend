import django_filters
from apps.task_management.models import AnsibleExecution, AnsibleTask

class AnsibleTaskFilter(django_filters.FilterSet):
    name = django_filters.CharFilter(lookup_expr='icontains')
    
    class Meta:
        model = AnsibleTask
        fields = ['task_type', 'resource_pool']

class AnsibleExecutionFilter(django_filters.FilterSet):
    task_name = django_filters.CharFilter(field_name='task__name', lookup_expr='icontains')
    status = django_filters.CharFilter(lookup_expr='exact')
    executor_name = django_filters.CharFilter(field_name='executor__username', lookup_expr='icontains')
    
    class Meta:
        model = AnsibleExecution
        fields = ['status', 'task']
