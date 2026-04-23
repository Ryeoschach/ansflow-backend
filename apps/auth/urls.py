from django.urls import path
from . import views

urlpatterns = [
    # 登出
    path('logout/', views.logout_view, name='logout'),

    # 三方登录（API JSON 方式）
    path('social/github/', views.github_login, name='github_login'),
    path('social/wechat/', views.wechat_login, name='wechat_login'),

    # 三方登录（前端 redirect 回调方式）
    path('social/wechat/callback/', views.WeChatCallbackView.as_view(), name='wechat_callback'),
    path('social/github/callback/', views.GitHubCallbackView.as_view(), name='github_callback'),

    # 已登录用户绑定第三方账号
    path('social/bind/', views.bind_social, name='bind_social'),

    # LDAP 登录
    path('ldap/login/', views.ldap_login, name='ldap_login'),
]
