# from django.contrib.auth.models import User # 已移除，改为动态获取以支持自定义模型
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView
from django.conf import settings
from rest_framework.response import Response
from django.contrib.auth import get_user_model


class CookieTokenObtainPairView(TokenObtainPairView):
    """
    登录视图：将 Refresh Token 存入 Cookie
    """

    def post(self, request, *args, **kwargs):
        response = super().post(request, *args, **kwargs)
        if response.status_code == 200:
            # 从数据中拿掉 refresh，不发给前端
            refresh_token = response.data.pop('refresh')

            # 获取用户信息一并返回
            User = get_user_model()
            user = User.objects.get(username=request.data.get('username'))
            user_data = {
                'username': user.username,
            }
            response.data['user'] = user_data

            print(f"DEBUG: Login success for {user_data['username']}, setting cookie.")

            # 设置 HttpOnly Cookie
            response.set_cookie(
                key='refresh_token',
                value=refresh_token,
                httponly=True,  # 保证 JS 无法读取
                secure=not settings.DEBUG,  # 只有生产环境 HTTPS 强制传输
                samesite='Lax',  # 基础 CSRF 保护
                # path='/api/v1/auth/refresh/',   # ！！！重要：限制该 Cookie 只发给特定的刷新接口，不污染其他请求
                path='/',   # ！！！重要：限制该 Cookie 只发给特定的刷新接口，不污染其他请求
                max_age=7 * 24 * 3600  # 与 settings 里的有效期一致
            )
        return response


class CookieTokenRefreshView(TokenRefreshView):
    """
    刷新视图：自动从 Cookie 中读取 Refresh Token
    """

    def post(self, request, *args, **kwargs):
        # 先尝试从 Cookie 中取
        refresh_token = request.COOKIES.get('refresh_token')

        # 调试日志
        if not refresh_token:
            print("DEBUG: Refresh request received but NO refresh_token cookie found!")
        else:
            print("DEBUG: Refresh request received with cookie.")

        # 如果存在，手动注入到 data 中（父类只会看 request.data['refresh']）
        data = request.data.copy() if hasattr(request.data, 'copy') else dict(request.data)
        if refresh_token:
            data['refresh'] = refresh_token

        serializer = self.get_serializer(data=data)
        try:
            serializer.is_valid(raise_exception=True)
        except Exception as e:
            print(f"DEBUG: Token serializer validation failed: {str(e)}")
            return Response({"detail": "Refresh token invalid", "error": str(e)}, status=401)

        # --- 检查用户是否还活着 ---
        from rest_framework_simplejwt.tokens import RefreshToken
        try:
            token_obj = RefreshToken(data.get('refresh'))
            user_id = token_obj.payload.get('user_id')
            print(f"DEBUG: Token belongs to user_id: {user_id}")
        except Exception as e:
            print(f"DEBUG: Token payload invalid: {str(e)}")
            return Response({"detail": "Token payload invalid"}, status=401)

        # 查库校验
        from django.contrib.auth import get_user_model
        User = get_user_model()
        user = User.objects.filter(id=user_id, is_active=True).first()
        if not user:
            print(f"DEBUG: User {user_id} is inactive or does not exist.")
            return Response({"detail": "用户已被禁用或不存在"}, status=401)

        response = Response(serializer.validated_data, status=200)

        if response.status_code == 200 and 'refresh' in response.data:
            new_refresh = response.data.pop('refresh')
            print("DEBUG: Setting NEW refresh_token cookie.")
            response.set_cookie(
                key='refresh_token',
                value=new_refresh,
                httponly=True,
                secure=not settings.DEBUG,
                samesite='Lax',
                path='/', # 保持一致，使用根路径
                max_age=7 * 24 * 3600
            )
        return response
