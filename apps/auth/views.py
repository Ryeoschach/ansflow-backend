"""
三方授权登录 + LDAP 认证视图
"""
import logging
import requests
from django.conf import settings
from django.http import HttpResponseRedirect, JsonResponse
from django.views import View
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import RefreshToken

from apps.rbac_permission.models import User

logger = logging.getLogger(__name__)


def get_tokens_for_user(user: User) -> dict:
    """为用户生成 JWT token 对"""
    refresh = RefreshToken.for_user(user)
    return {
        'access_token': str(refresh.access_token),
        'refresh_token': str(refresh),
    }


def _find_or_create_github_user(github_id: str, gh_username: str) -> User:
    """GitHub 用户查找或创建"""
    user = User.objects.filter(github_id=github_id).first()
    if not user:
        base_username = f"gh_{gh_username}"
        username = base_username
        counter = 1
        while User.objects.filter(username=username).exists():
            username = f"{base_username}_{counter}"
            counter += 1
        user = User.objects.create_user(username=username, login_type='github', github_id=github_id)
        logger.info(f"[GitHub Login] 新建用户: {username} (github_id={github_id})")
    if user.login_type != 'github':
        user.login_type = 'github'
        user.save(update_fields=['login_type', 'update_time'])
    return user


def _find_or_create_wechat_user(openid: str, nickname: str = '', headimgurl: str = '') -> User:
    """微信用户查找或创建"""
    user = User.objects.filter(wechat_openid=openid).first()
    if not user:
        base_username = f"wx_{openid[:8]}"
        if nickname:
            # 优先使用昵称
            base_username = f"wx_{nickname[:20]}"
        username = base_username
        counter = 1
        while User.objects.filter(username=username).exists():
            username = f"{base_username}_{counter}"
            counter += 1
        user = User.objects.create_user(username=username, login_type='wechat', wechat_openid=openid)
        logger.info(f"[WeChat Login] 新建用户: {username} (openid={openid})")
    if user.login_type != 'wechat':
        user.login_type = 'wechat'
        user.save(update_fields=['login_type', 'update_time'])
    return user


# ============================================================
# 微信扫码登录回调（前端 redirect 过来）
# GET /api/v1/auth/social/wechat/callback/?code=xxx&redirect_uri=https://xxx
# ============================================================
class WeChatCallbackView(View):
    """微信 OAuth2 网页应用扫码登录回调"""

    def get(self, request):
        code = request.GET.get('code')
        redirect_uri = request.GET.get('redirect_uri', '/')

        if not code:
            return JsonResponse({'error': '缺少 code'}, status=400)

        appid = getattr(settings, 'WECHAT_APPID', None)
        appsecret = getattr(settings, 'WECHAT_APPSECRET', None)

        if not appid or not appsecret:
            return JsonResponse({'error': '微信 OAuth 未配置'}, status=503)

        # 1. 用 code 换取 access_token（网页应用接口）
        token_resp = requests.get(
            'https://api.weixin.qq.com/sns/oauth2/access_token',
            params={
                'appid': appid,
                'secret': appsecret,
                'code': code,
                'grant_type': 'authorization_code',
            },
            timeout=10,
        )
        token_data = token_resp.json()
        access_token = token_data.get('access_token')
        openid = token_data.get('openid')

        if not access_token or not openid:
            return JsonResponse({'error': '微信 AccessToken 获取失败', 'detail': token_data}, status=400)

        # 2. 获取用户信息（昵称、头像等，可选）
        user_info_resp = requests.get(
            'https://api.weixin.qq.com/sns/userinfo',
            params={'access_token': access_token, 'openid': openid},
            timeout=10,
        )
        user_info = user_info_resp.json()
        nickname = user_info.get('nickname', '')
        headimgurl = user_info.get('headimgurl', '')

        # 3. 查找或创建用户
        user = _find_or_create_wechat_user(openid, nickname, headimgurl)
        tokens = get_tokens_for_user(user)

        # 4. 设置 refresh token 到 HttpOnly cookie，重定向到前端
        separator = '&' if '?' in redirect_uri else '?'
        redirect_url = (
            f"{redirect_uri}{separator}"
            f"access_token={tokens['access_token']}&"
            f"username={user.username}&"
            f"user_id={user.id}"
        )
        response = HttpResponseRedirect(redirect_url)
        response.set_cookie(
            key='refresh_token',
            value=tokens['refresh_token'],
            httponly=True,
            secure=not settings.DEBUG,
            samesite='Lax',
            path='/',
            max_age=7 * 24 * 3600
        )
        return response


# ============================================================
# GitHub 授权回调
# GET /api/v1/auth/social/github/callback/?code=xxx&redirect_uri=https://xxx
# ============================================================
class GitHubCallbackView(View):
    """GitHub OAuth 回调"""

    def get(self, request):
        code = request.GET.get('code')
        redirect_uri = request.GET.get('redirect_uri', '/')

        if not code:
            return JsonResponse({'error': '缺少 code'}, status=400)

        client_id = getattr(settings, 'GITHUB_CLIENT_ID', None)
        client_secret = getattr(settings, 'GITHUB_CLIENT_SECRET', None)

        if not client_id or not client_secret:
            return JsonResponse({'error': 'GitHub OAuth 未配置'}, status=503)

        # code 换 Access Token
        token_resp = requests.post(
            'https://github.com/login/oauth/access_token',
            json={'client_id': client_id, 'client_secret': client_secret, 'code': code},
            headers={'Accept': 'application/json'},
            timeout=10,
        )
        token_data = token_resp.json()
        access_token = token_data.get('access_token')
        if not access_token:
            return JsonResponse({'error': 'GitHub Access Token 获取失败'}, status=400)

        # 用 Access Token 获取用户信息
        user_resp = requests.get(
            'https://api.github.com/user',
            headers={
                'Authorization': f'Bearer {access_token}',
                'Accept': 'application/vnd.github.v3+json',
            },
            timeout=10,
        )
        if user_resp.status_code != 200:
            return JsonResponse({'error': 'GitHub 用户信息获取失败'}, status=400)

        gh_user = user_resp.json()
        github_id = str(gh_user.get('id'))
        gh_username = gh_user.get('login', '')

        user = _find_or_create_github_user(github_id, gh_username)
        tokens = get_tokens_for_user(user)

        # 设置 refresh token 到 HttpOnly cookie
        separator = '&' if '?' in redirect_uri else '?'
        redirect_url = (
            f"{redirect_uri}{separator}"
            f"access_token={tokens['access_token']}&"
            f"username={user.username}&"
            f"user_id={user.id}"
        )
        response = HttpResponseRedirect(redirect_url)
        response.set_cookie(
            key='refresh_token',
            value=tokens['refresh_token'],
            httponly=True,
            secure=not settings.DEBUG,
            samesite='Lax',
            path='/',
            max_age=7 * 24 * 3600
        )
        return response


@api_view(['POST'])
@permission_classes([AllowAny])
def github_login(request):
    """
    GitHub OAuth 登录
    POST /api/v1/auth/social/github/
    Body: { code: "github-authorization-code" }
    """
    code = request.data.get('code')
    if not code:
        return Response({'error': '缺少 code 参数'}, status=status.HTTP_400_BAD_REQUEST)

    # 1. 用 code 换 GitHub Access Token
    client_id = getattr(settings, 'GITHUB_CLIENT_ID', None)
    client_secret = getattr(settings, 'GITHUB_CLIENT_SECRET', None)

    if not client_id or not client_secret:
        return Response({'error': 'GitHub OAuth 未配置'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

    token_resp = requests.post(
        'https://github.com/login/oauth/access_token',
        json={
            'client_id': client_id,
            'client_secret': client_secret,
            'code': code,
        },
        headers={'Accept': 'application/json'},
        timeout=10,
    )
    token_data = token_resp.json()
    access_token = token_data.get('access_token')
    if not access_token:
        return Response({'error': 'GitHub Access Token 获取失败', 'detail': token_data}, status=status.HTTP_400_BAD_REQUEST)

    # 2. 用 Access Token 获取用户信息
    user_resp = requests.get(
        'https://api.github.com/user',
        headers={
            'Authorization': f'Bearer {access_token}',
            'Accept': 'application/vnd.github.v3+json',
        },
        timeout=10,
    )
    if user_resp.status_code != 200:
        return Response({'error': 'GitHub 用户信息获取失败'}, status=status.HTTP_400_BAD_REQUEST)

    gh_user = user_resp.json()
    github_id = str(gh_user.get('id'))
    gh_username = gh_user.get('login', '')

    # 3. 查找或创建用户
    user = User.objects.filter(github_id=github_id).first()
    if not user:
        # 自动创建用户，username 加前缀避免冲突
        base_username = f"gh_{gh_username}"
        username = base_username
        counter = 1
        while User.objects.filter(username=username).exists():
            username = f"{base_username}_{counter}"
            counter += 1

        user = User.objects.create_user(
            username=username,
            login_type='github',
            github_id=github_id,
        )
        logger.info(f"[GitHub Login] 新建用户: {username} (github_id={github_id})")

    # 4. 更新登录方式
    if user.login_type != 'github':
        user.login_type = 'github'
        user.save(update_fields=['login_type', 'update_time'])

    tokens = get_tokens_for_user(user)
    return Response({
        'message': '登录成功',
        'user': {
            'id': user.id,
            'username': user.username,
            'login_type': user.login_type,
        },
        **tokens,
    })


@api_view(['POST'])
@permission_classes([AllowAny])
def wechat_login(request):
    """
    微信 OAuth2 登录
    POST /api/v1/auth/social/wechat/
    Body: { code: "wx-authorization-code" }
    """
    code = request.data.get('code')
    if not code:
        return Response({'error': '缺少 code 参数'}, status=status.HTTP_400_BAD_REQUEST)

    appid = getattr(settings, 'WECHAT_APPID', None)
    appsecret = getattr(settings, 'WECHAT_APPSECRET', None)

    if not appid or not appsecret:
        return Response({'error': '微信 OAuth 未配置'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

    # 1. 用 code 换 OpenID + AccessToken
    wx_resp = requests.get(
        'https://api.weixin.qq.com/sns/jscode2session',
        params={
            'appid': appid,
            'secret': appsecret,
            'js_code': code,
            'grant_type': 'authorization_code',
        },
        timeout=10,
    )
    wx_data = wx_resp.json()
    openid = wx_data.get('openid')
    if not openid:
        return Response({'error': '微信 OpenID 获取失败', 'detail': wx_data}, status=status.HTTP_400_BAD_REQUEST)

    # 2. 查找或创建用户
    user = User.objects.filter(wechat_openid=openid).first()
    if not user:
        base_username = f"wx_{openid[:8]}"
        username = base_username
        counter = 1
        while User.objects.filter(username=username).exists():
            username = f"{base_username}_{counter}"
            counter += 1

        user = User.objects.create_user(
            username=username,
            login_type='wechat',
            wechat_openid=openid,
        )
        logger.info(f"[WeChat Login] 新建用户: {username} (openid={openid})")

    if user.login_type != 'wechat':
        user.login_type = 'wechat'
        user.save(update_fields=['login_type', 'update_time'])

    tokens = get_tokens_for_user(user)
    return Response({
        'message': '登录成功',
        'user': {
            'id': user.id,
            'username': user.username,
            'login_type': user.login_type,
        },
        **tokens,
    })


@api_view(['POST'])
@permission_classes([AllowAny])
def ldap_login(request):
    """
    LDAP 认证登录
    POST /api/v1/auth/ldap/login/
    Body: { username: "john", password: "xxx" }
    """
    username = request.data.get('username')
    password = request.data.get('password')

    if not username or not password:
        return Response({'error': '用户名和密码不能为空'}, status=status.HTTP_400_BAD_REQUEST)

    ldap_server = getattr(settings, 'LDAP_SERVER', None)
    ldap_base_dn = getattr(settings, 'LDAP_BASE_DN', None)
    ldap_user_dn_template = getattr(settings, 'LDAP_USER_DN_TEMPLATE', 'uid={username},' + (ldap_base_dn or ''))

    if not ldap_server:
        return Response({'error': 'LDAP 未配置'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

    try:
        import ldap
    except ImportError:
        return Response({'error': 'LDAP 模块未安装'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

    try:
        # 1. LDAP 连接认证
        conn = ldap.initialize(ldap_server)
        conn.set_option(ldap.OPT_REFERRALS, 0)
        conn.protocol_version = ldap.VERSION3

        user_dn = ldap_user_dn_template.format(username=username)
        conn.simple_bind_s(user_dn, password)
        logger.info(f"[LDAP Login] 认证成功: {user_dn}")

        # 2. 查询用户信息
        search_filter = f'(uid={username})'
        result = conn.search_s(ldap_base_dn, ldap.SCOPE_SUBTREE, search_filter, ['cn', 'mail', 'uid', 'displayName'])
        user_info = {}
        if result:
            _, attrs = result[0]
            user_info = {
                'cn': attrs.get('cn', [b''])[0].decode('utf-8') if attrs.get('cn') else '',
                'mail': attrs.get('mail', [b''])[0].decode('utf-8') if attrs.get('mail') else '',
                'uid': attrs.get('uid', [b''])[0].decode('utf-8') if attrs.get('uid') else '',
            }

        conn.unbind_s()

        # 3. 查找或创建本地用户
        user = User.objects.filter(ldap_uid=username).first()
        if not user:
            base_username = username
            username_actual = base_username
            counter = 1
            while User.objects.filter(username=username_actual).exists():
                username_actual = f"{base_username}_{counter}"
                counter += 1

            user = User.objects.create_user(
                username=username_actual,
                login_type='ldap',
                ldap_uid=username,
                ldap_dn=user_dn,
                email=user_info.get('mail', ''),
            )
            logger.info(f"[LDAP Login] 新建用户: {username_actual} (ldap_uid={username})")

        if user.login_type != 'ldap':
            user.login_type = 'ldap'
            user.save(update_fields=['login_type', 'update_time'])

        tokens = get_tokens_for_user(user)
        return Response({
            'message': '登录成功',
            'user': {
                'id': user.id,
                'username': user.username,
                'login_type': user.login_type,
                'email': user.email,
            },
            **tokens,
        })

    except ldap.INVALID_CREDENTIALS:
        return Response({'error': '用户名或密码错误'}, status=status.HTTP_401_UNAUTHORIZED)
    except Exception as e:
        logger.error(f"[LDAP Login] 错误: {str(e)}")
        return Response({'error': f'LDAP 认证失败: {str(e)}'}, status=status.HTTP_401_UNAUTHORIZED)


@api_view(['POST'])
@permission_classes([AllowAny])
def logout_view(request):
    """
    登出 - 清除 refresh token cookie
    POST /api/v1/auth/logout/
    """
    response = Response({'message': '登出成功'})
    response.delete_cookie('refresh_token', path='/')
    return response


@api_view(['POST'])
@permission_classes([AllowAny])
def bind_social(request):
    """
    已登录用户绑定第三方账号
    POST /api/v1/auth/social/bind/
    Body: { provider: "github"|"wechat", code: "..." }
    """
    user = request.user
    if not user or user.is_anonymous:
        return Response({'error': '未登录'}, status=status.HTTP_401_UNAUTHORIZED)

    provider = request.data.get('provider')
    code = request.data.get('code')

    if provider == 'github':
        # 实现同 GitHub 登录流程，但关联到当前用户
        client_id = getattr(settings, 'GITHUB_CLIENT_ID', None)
        client_secret = getattr(settings, 'GITHUB_CLIENT_SECRET', None)

        token_resp = requests.post(
            'https://github.com/login/oauth/access_token',
            json={'client_id': client_id, 'client_secret': client_secret, 'code': code},
            headers={'Accept': 'application/json'},
            timeout=10,
        )
        token_data = token_resp.json()
        access_token = token_data.get('access_token')
        if not access_token:
            return Response({'error': 'GitHub Access Token 获取失败'}, status=status.HTTP_400_BAD_REQUEST)

        user_resp = requests.get(
            'https://api.github.com/user',
            headers={'Authorization': f'Bearer {access_token}', 'Accept': 'application/vnd.github.v3+json'},
            timeout=10,
        )
        gh_user = user_resp.json()
        github_id = str(gh_user.get('id'))

        if User.objects.filter(github_id=github_id).exclude(id=user.id).exists():
            return Response({'error': '该 GitHub 账号已被其他用户绑定'}, status=status.HTTP_409_CONFLICT)

        user.github_id = github_id
        user.login_type = 'github'
        user.save(update_fields=['github_id', 'login_type', 'update_time'])
        return Response({'message': 'GitHub 账号绑定成功'})

    elif provider == 'wechat':
        appid = getattr(settings, 'WECHAT_APPID', None)
        appsecret = getattr(settings, 'WECHAT_APPSECRET', None)

        wx_resp = requests.get(
            'https://api.weixin.qq.com/sns/jscode2session',
            params={'appid': appid, 'secret': appsecret, 'js_code': code, 'grant_type': 'authorization_code'},
            timeout=10,
        )
        wx_data = wx_resp.json()
        openid = wx_data.get('openid')
        if not openid:
            return Response({'error': '微信 OpenID 获取失败'}, status=status.HTTP_400_BAD_REQUEST)

        if User.objects.filter(wechat_openid=openid).exclude(id=user.id).exists():
            return Response({'error': '该微信账号已被其他用户绑定'}, status=status.HTTP_409_CONFLICT)

        user.wechat_openid = openid
        user.login_type = 'wechat'
        user.save(update_fields=['wechat_openid', 'login_type', 'update_time'])
        return Response({'message': '微信账号绑定成功'})

    return Response({'error': '不支持的 provider'}, status=status.HTTP_400_BAD_REQUEST)
