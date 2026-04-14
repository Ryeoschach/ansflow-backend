from django_filters import rest_framework as filters
from apps.host_management.models import Host, ResourcePool, Environment, Platform

class HostFilter(filters.FilterSet):
    hostname = filters.CharFilter(lookup_expr='icontains')
    private_ip = filters.CharFilter(lookup_expr='icontains')
    ip_address = filters.CharFilter(lookup_expr='icontains')
    
    class Meta:
        model = Host
        fields = ['env', 'platform', 'status', 'hostname', 'private_ip', 'ip_address']

class ResourcePoolFilter(filters.FilterSet):
    name = filters.CharFilter(lookup_expr='icontains')
    code = filters.CharFilter(lookup_expr='icontains')
    
    # 联合查询过滤：过滤包含特定环境或平台主机的资源池
    env = filters.ModelChoiceFilter(
        field_name='hosts__env',
        queryset=Environment.objects.all(),
        label='环境'
    )
    platform = filters.ModelChoiceFilter(
        field_name='hosts__platform',
        queryset=Platform.objects.all(),
        label='平台'
    )

    class Meta:
        model = ResourcePool
        fields = ['name', 'code', 'env', 'platform']
