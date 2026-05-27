import json
from channels.generic.websocket import AsyncWebsocketConsumer
from rest_framework_simplejwt.tokens import AccessToken
from django.contrib.auth import get_user_model
from channels.db import database_sync_to_async

User = get_user_model()

class NotificationConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        # We accept the connection first
        await self.accept()
        self.authenticated = False

    async def disconnect(self, close_code):
        if getattr(self, 'authenticated', False):
            await self.channel_layer.group_discard(
                self.group_name,
                self.channel_name
            )

    async def receive(self, text_data):
        try:
            data = json.loads(text_data)
        except Exception:
            return

        if data.get('type') == 'auth':
            token_str = data.get('token')
            if not token_str:
                await self.send(text_data=json.dumps({"type": "auth_result", "status": "failed", "message": "Missing token"}))
                await self.close()
                return

            # Verify token
            user = await self.verify_token(token_str)
            if not user:
                await self.send(text_data=json.dumps({"type": "auth_result", "status": "failed", "message": "Invalid token"}))
                await self.close()
                return

            self.user = user
            self.group_name = f"user_notifications_{user.id}"
            self.authenticated = True

            await self.channel_layer.group_add(
                self.group_name,
                self.channel_name
            )
            await self.send(text_data=json.dumps({"type": "auth_result", "status": "success"}))

    @database_sync_to_async
    def verify_token(self, token_str):
        try:
            access_token = AccessToken(token_str)
            user_id = access_token['user_id']
            return User.objects.get(id=user_id)
        except Exception:
            return None

    async def send_notification(self, event):
        if getattr(self, 'authenticated', False):
            await self.send(text_data=json.dumps({
                "type": "notification",
                "data": event["data"]
            }))
