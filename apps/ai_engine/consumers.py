import json
import logging
from urllib.parse import parse_qs
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from channels.db import database_sync_to_async
from asgiref.sync import sync_to_async

logger = logging.getLogger(__name__)

@database_sync_to_async
def get_authenticated_user(token_string):
    if not token_string:
        return None
    try:
        from rest_framework_simplejwt.tokens import AccessToken
        from django.contrib.auth import get_user_model
        access_token = AccessToken(token_string)
        user_id = access_token['user_id']
        User = get_user_model()
        return User.objects.get(id=user_id)
    except Exception as e:
        logger.error(f"[WS-AI] Failed to get user from token: {e}")
        return None

@database_sync_to_async
def resolve_and_verify_ws_project(user, project_id=None):
    if not user:
        return None, False
    from apps.rbac_permission.models import Project, ProjectMember
    project = None
    if project_id:
        if str(project_id).isdigit():
            project = Project.objects.filter(id=project_id).first()
        if not project:
            project = Project.objects.filter(code=project_id).first()
            
    if not project:
        project = Project.objects.filter(code='default').first()
        if not project:
            first_membership = ProjectMember.objects.filter(user=user).select_related('project').first()
            if first_membership:
                project = first_membership.project
            else:
                project = Project.objects.first()
                
    if not project:
        return None, False
        
    if user.is_superuser:
        return project, True
        
    has_membership = ProjectMember.objects.filter(project=project, user=user).exists()
    return project, has_membership

@database_sync_to_async
def create_chat_message_record(history_id, role, content, metadata=None):
    try:
        from apps.ai_engine.models import AIChatMessage, AIChatHistory
        history = AIChatHistory.objects.get(id=history_id)
        msg = AIChatMessage.objects.create(
            history=history,
            role=role,
            content=content,
            metadata=metadata or {}
        )
        return msg.id
    except Exception as e:
        logger.error(f"[WS-AI] Failed to create chat message: {e}")
        return None

class AIChatConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        # 1. 提取 Query Params
        query_string = self.scope.get('query_string', b'').decode('utf-8')
        params = parse_qs(query_string)
        token = params.get('token', [None])[0]
        project_id = params.get('project_id', [None])[0]

        # 2. 身份验证
        self.user = await get_authenticated_user(token)
        if not self.user:
            logger.warning("[WS-AI] Rejecting connection: Unauthenticated.")
            await self.close(code=4001)
            return

        # 3. 校验并解析项目
        self.project, is_member = await resolve_and_verify_ws_project(self.user, project_id)
        if not is_member or not self.project:
            logger.warning(f"[WS-AI] Rejecting connection: User {self.user.username} has no access to project {project_id or 'default'}.")
            await self.close(code=4003)
            return

        self.project_id = self.project.id

        # 4. 连接建立
        await self.accept()
        logger.info(f"[WS-AI] Connection accepted for user: {self.user.username}, project: {self.project_id}")

    async def disconnect(self, close_code):
        logger.info(f"[WS-AI] Connection closed with code: {close_code}")

    async def receive_json(self, content, **kwargs):
        question = content.get('question')
        history_id = content.get('history_id')
        personality = content.get('personality', 'professional')
        llm_id = content.get('llm_id')
        embedding_id = content.get('embedding_id')

        if not question or not history_id:
            await self.send_json({"type": "error", "message": "question and history_id are required"})
            return

        # 1. 保存用户的消息
        await create_chat_message_record(history_id, 'user', question)

        # 2. 构造权限感知上下文
        from apps.ai_engine.utils import get_authorized_resources
        # 传入 project_id 以限定数据权限作用域
        auth_context = await sync_to_async(get_authorized_resources)(self.user, project_id=self.project_id)

        # 3. 初始化 RAGService
        from apps.ai_engine.rag_service import RAGService
        try:
            # RAGService 的 __init__ 中包含数据库查询，必须在同步上下文中执行
            rag_service = await sync_to_async(RAGService)(
                personality=personality,
                llm_id=llm_id,
                embedding_id=embedding_id
            )
        except Exception as err:
            await self.send_json({"type": "error", "message": f"Initialization failed: {str(err)}"})
            return

        # 4. 流式生成与推送
        await self.send_json({"type": "start"})
        
        full_response = ""
        iterator = iter(rag_service.chat_stream(question, history_id=history_id, auth_context=auth_context))
        
        sentinel = object()
        def safe_next(it):
            try:
                return next(it)
            except StopIteration:
                return sentinel

        async def get_next_chunk():
            try:
                res = await sync_to_async(safe_next)(iterator)
                if res is sentinel:
                    return None
                return res
            except Exception as e:
                logger.error(f"[WS-AI] Error during LLM stream iteration: {e}")
                return f"\n[AI Error] {str(e)}"

        while True:
            chunk = await get_next_chunk()
            if chunk is None:
                break
            if chunk.startswith("\n[AI Error]"):
                await self.send_json({"type": "error", "message": chunk})
                break
            
            full_response += chunk
            await self.send_json({"type": "chunk", "text": chunk})

        # 5. 后处理与参考文档元数据提取
        import re
        referenced_docs = []
        clean_content = full_response
        
        ref_match = re.search(r'__REFERENCES__:(\[.*?\])\n', full_response)
        if ref_match:
            try:
                referenced_docs = json.loads(ref_match.group(1))
                clean_content = re.sub(r'__REFERENCES__:\[.*?\]\n', '', full_response).strip()
            except Exception:
                pass

        # 6. 保存助手的消息
        msg_metadata = {'referenced_docs': referenced_docs} if referenced_docs else {}
        msg_id = await create_chat_message_record(history_id, 'assistant', clean_content, metadata=msg_metadata)

        await self.send_json({
            "type": "end",
            "message_id": msg_id,
            "referenced_docs": referenced_docs
        })
