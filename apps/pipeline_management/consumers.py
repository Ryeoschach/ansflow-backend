import json
from channels.generic.websocket import AsyncWebsocketConsumer

class PipelineConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.run_id = self.scope['url_route']['kwargs']['run_id']
        self.room_group_name = f'pipeline_run_{self.run_id}'

        # TODO: 后续可以增加 JWT 校验逻辑（从 self.scope['headers'] 中提权）

        # 加入组
        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name
        )

        await self.accept()

    async def disconnect(self, close_code):
        # 离开组
        await self.channel_layer.group_discard(
            self.room_group_name,
            self.channel_name
        )

    # 接收来自组的消息（由 tasks.py 触发）
    async def pipeline_run_update(self, event):
        data = event['data']

        # 发送到 WebSocket
        await self.send(text_data=json.dumps({
            'type': 'status_update',
            'data': data
        }))

class PipelineListConsumer(AsyncWebsocketConsumer):
    """
    为流水线列表页/仪表盘提供的全局消息消费者
    """
    async def connect(self):
        self.room_group_name = 'pipeline_all'
        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name
        )
        await self.accept()

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(
            self.room_group_name,
            self.channel_name
        )

    # 接收来自全局组的消息
    async def pipeline_all_update(self, event):
        data = event['data']
        await self.send(text_data=json.dumps({
            'type': 'all_status_update',
            'data': data
        }))
