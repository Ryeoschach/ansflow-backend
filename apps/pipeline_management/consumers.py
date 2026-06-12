import json
from channels.generic.websocket import AsyncWebsocketConsumer
from utils.websocket_auth import (
    authenticate_websocket,
    authorize_pipeline_event,
    authorize_pipeline_run,
    authorize_pipeline_stream,
    get_query_param,
)

class PipelineConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.run_id = self.scope['url_route']['kwargs']['run_id']
        self.room_group_name = f'pipeline_run_{self.run_id}'
        self.user = await authenticate_websocket(self.scope)
        project_id = get_query_param(self.scope, 'project_id')

        if not self.user:
            await self.close(code=4001)
            return
        if not await authorize_pipeline_run(self.user, self.run_id, project_id):
            await self.close(code=4003)
            return

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
        self.user = await authenticate_websocket(self.scope)
        requested_project_id = get_query_param(self.scope, 'project_id')
        if not self.user:
            await self.close(code=4001)
            return

        self.project_id = await authorize_pipeline_stream(
            self.user, requested_project_id
        )
        if not self.project_id:
            await self.close(code=4003)
            return

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
        run_id = data.get('id')
        if not run_id or not await authorize_pipeline_event(
            self.user, run_id, self.project_id
        ):
            return
        await self.send(text_data=json.dumps({
            'type': 'all_status_update',
            'data': data
        }))
