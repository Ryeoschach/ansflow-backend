import json
import threading
from channels.generic.websocket import AsyncWebsocketConsumer
from asgiref.sync import async_to_sync
from kubernetes import client as k8s_client
from kubernetes.stream import stream
from .utils.k8s_helper import get_k8s_client
from urllib.parse import parse_qs
from utils.websocket_auth import authenticate_websocket, authorize_k8s_cluster

class K8sTerminalConsumer(AsyncWebsocketConsumer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.k8s_stream = None
        self.thread = None
        self.keep_running = True

    async def connect(self):
        # 1. 获取 URL 参数
        self.cluster_id = self.scope['url_route']['kwargs']['cluster_id']
        self.namespace = self.scope['url_route']['kwargs']['namespace']
        self.pod_name = self.scope['url_route']['kwargs']['pod_name']
        
        # 2. 获取 Query String 中的 container
        query_string = self.scope.get('query_string', b'').decode()
        params = parse_qs(query_string)
        self.container = params.get('container', [None])[0]

        # 3. 鉴权
        self.user = await authenticate_websocket(self.scope)
        if not self.user:
            await self.close(code=4001)
            return
        project_id = params.get('project_id', [None])[0]
        self.cluster = await authorize_k8s_cluster(
            self.user,
            self.cluster_id,
            project_id,
            permission_code='k8s:cluster:pod_exec',
        )
        if not self.cluster:
            await self.close(code=4003)
            return

        await self.accept()

        self.thread = threading.Thread(target=self.run_k8s_stream)
        self.thread.daemon = True
        self.thread.start()

    async def disconnect(self, close_code):
        self.keep_running = False
        if self.k8s_stream:
            try:
                self.k8s_stream.close()
            except:
                pass

    async def receive(self, text_data=None, bytes_data=None):
        if text_data:
            data = json.loads(text_data)
            if data.get('type') == 'terminal' and self.k8s_stream and self.k8s_stream.is_open():
                self.k8s_stream.write_stdin(data.get('data'))
            elif data.get('type') == 'resize' and self.k8s_stream and self.k8s_stream.is_open():
                cols = data.get('cols')
                rows = data.get('rows')
                try:
                    self.k8s_stream.write_channel(4, json.dumps({"Height": rows, "Width": cols}))
                except:
                    pass

    def run_k8s_stream(self):
        try:
            api_client = get_k8s_client(self.cluster)
            core_api = k8s_client.CoreV1Api(api_client)

            if not self.container:
                pod_obj = core_api.read_namespaced_pod(self.pod_name, self.namespace)
                if pod_obj.spec.containers:
                    self.container = pod_obj.spec.containers[0].name

            exec_command = ['/bin/sh', '-c', 'TERM=xterm-256color /bin/sh']
            
            self.k8s_stream = stream(
                core_api.connect_get_namespaced_pod_exec,
                self.pod_name,
                self.namespace,
                container=self.container,
                command=exec_command,
                stderr=True, stdin=True, stdout=True, tty=True,
                _preload_content=False
            )

            while self.keep_running and self.k8s_stream.is_open():
                resp = self.k8s_stream.read_stdout(timeout=1)
                if resp:
                    async_to_sync(self.send)(text_data=json.dumps({
                        'type': 'terminal',
                        'data': resp
                    }))
                
                err = self.k8s_stream.read_stderr(timeout=1)
                if err:
                    async_to_sync(self.send)(text_data=json.dumps({
                        'type': 'terminal',
                        'data': err
                    }))

        except Exception as e:
            async_to_sync(self.send)(text_data=json.dumps({
                'type': 'error',
                'data': f"K8s Terminal Error: {str(e)}"
            }))
        finally:
            self.keep_running = False
            async_to_sync(self.close)()

class K8sLogConsumer(AsyncWebsocketConsumer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.log_stream = None
        self.thread = None
        self.keep_running = True

    async def connect(self):
        self.cluster_id = self.scope['url_route']['kwargs']['cluster_id']
        self.namespace = self.scope['url_route']['kwargs']['namespace']
        self.pod_name = self.scope['url_route']['kwargs']['pod_name']
        
        query_string = self.scope.get('query_string', b'').decode()
        params = parse_qs(query_string)
        self.container = params.get('container', [None])[0]
        self.tail_lines = int(params.get('tail_lines', [100])[0])

        self.user = await authenticate_websocket(self.scope)
        if not self.user:
            await self.close(code=4001)
            return
        project_id = params.get('project_id', [None])[0]
        self.cluster = await authorize_k8s_cluster(
            self.user,
            self.cluster_id,
            project_id,
            permission_code='k8s:cluster:resources_view',
        )
        if not self.cluster:
            await self.close(code=4003)
            return

        await self.accept()

        self.thread = threading.Thread(target=self.run_log_stream)
        self.thread.daemon = True
        self.thread.start()

    async def disconnect(self, close_code):
        self.keep_running = False

    def run_log_stream(self):
        try:
            api_client = get_k8s_client(self.cluster)
            core_api = k8s_client.CoreV1Api(api_client)

            # 建立 Log Stream
            self.log_stream = core_api.read_namespaced_pod_log(
                name=self.pod_name,
                namespace=self.namespace,
                container=self.container,
                follow=True,
                _preload_content=False,
                tail_lines=self.tail_lines
            )

            for line in self.log_stream:
                if not self.keep_running:
                    break
                if line:
                    async_to_sync(self.send)(text_data=json.dumps({
                        'type': 'log',
                        'data': line.decode('utf-8', errors='replace')
                    }))

        except Exception as e:
            async_to_sync(self.send)(text_data=json.dumps({
                'type': 'error',
                'data': f"K8s Log Error: {str(e)}"
            }))
        finally:
            self.keep_running = False
            async_to_sync(self.close)()
