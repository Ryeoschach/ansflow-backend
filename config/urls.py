"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from drf_spectacular.views import SpectacularAPIView, SpectacularRedocView, SpectacularSwaggerView


from .api_router import api_v1_patterns

urlpatterns = [
    # 全局 API 版本入口
    path('api/v1/', include(api_v1_patterns)),
    # <str:version> 会被 DRF 的 URLPathVersioning 自动捕捉
    # path('api/<str:version>/', include(api_v1_patterns)),
]

if getattr(settings, 'ENABLE_ADMIN', False) or settings.DEBUG:
    urlpatterns += [
        path('admin/', admin.site.urls),

        # 生成 Schema 描述文件 (JSON 格式)
        path('api/schema/', SpectacularAPIView.as_view(), name='schema'),

        # Swagger UI (交互式界面)
        path('api/docs/', SpectacularSwaggerView.as_view(url_name='schema'), name='swagger-ui'),

        # ReDoc (展示类界面)
        path('api/redoc/', SpectacularRedocView.as_view(url_name='schema'), name='redoc'),
    ]