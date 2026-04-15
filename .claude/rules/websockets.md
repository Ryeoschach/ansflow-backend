---
paths:
  - "apps/**/consumers.py"
  - "config/routing.py"
  - "config/asgi.py"
---

# WebSocket 规则

## Channels 配置

ASGI 应用入口: `config/asgi.py`

路由: `config/routing.py`

## Consumer 规范

```python
import json
from channels.generic.websocket import AsyncWebsocketConsumer

class PipelineLogConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.run_id = self.scope['url_route']['kwargs']['run_id']
        self.room_group_name = f'pipeline_{self.run_id}'

        # 加入房间组
        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name
        )
        await self.accept()

    async def disconnect(self, close_code):
        # 离开房间组
        await self.channel_layer.group_discard(
            self.room_group_name,
            self.channel_name
        )

    async def receive(self, text_data):
        # 处理客户端消息
        data = json.loads(text_data)
        await self.channel_layer.group_send(
            self.room_group_name,
            {'type': 'log_message', 'data': data}
        )

    async def log_message(self, event):
        # 发送消息到 WebSocket
        await self.send(text_data=json.dumps(event['data']))
```

## 消息格式

统一使用 JSON:

```json
{
  "type": "log",
  "data": {
    "node_id": "xxx",
    "content": "Building...",
    "timestamp": "2024-01-01T00:00:00Z"
  }
}
```

## Channel Layer

使用 Redis-backed Channel Layer (`channels_redis`):

```python
CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {
            "hosts": [("127.0.0.1", 6379)],
        },
    },
}
```

## 从 View 推送消息

```python
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync

def push_log(run_id, log_data):
    channel_layer = get_channel_layer()
    async_to_sync(channel_layer.group_send)(
        f'pipeline_{run_id}',
        {'type': 'log_message', 'data': log_data}
    )
```

## 不要做的事

- **不要**在 Consumer 中直接操作数据库，使用 async 方法或线程
- **不要**发送过大的消息，分页传输
- **不要**在 WebSocket 连接中处理长时间阻塞操作
