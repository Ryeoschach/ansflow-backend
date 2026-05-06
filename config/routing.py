from django.urls import re_path
from apps.pipeline_management import consumers as pipeline_consumers
from apps.k8s_management import consumers as k8s_consumers

websocket_urlpatterns = [
    re_path(r'ws/pipeline/all/$', pipeline_consumers.PipelineListConsumer.as_asgi()),
    re_path(r'ws/pipeline/(?P<run_id>\w+)/$', pipeline_consumers.PipelineConsumer.as_asgi()),
    re_path(r'ws/k8s/(?P<cluster_id>\d+)/terminal/(?P<namespace>[\w-]+)/(?P<pod_name>[\w-]+)/$', k8s_consumers.K8sTerminalConsumer.as_asgi()),
    re_path(r'ws/k8s/(?P<cluster_id>\d+)/logs/(?P<namespace>[\w-]+)/(?P<pod_name>[\w-]+)/$', k8s_consumers.K8sLogConsumer.as_asgi()),
]
