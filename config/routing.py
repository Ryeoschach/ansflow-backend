from django.urls import re_path
from apps.pipeline_management import consumers

websocket_urlpatterns = [
    re_path(r'ws/pipeline/all/$', consumers.PipelineListConsumer.as_asgi()),
    re_path(r'ws/pipeline/(?P<run_id>\w+)/$', consumers.PipelineConsumer.as_asgi()),
    # re_path(r'ws/k8s/pods/$', consumers.PodConsumer.as_asgi()),
]
