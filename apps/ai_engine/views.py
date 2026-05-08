from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from django.http import StreamingHttpResponse
from drf_spectacular.utils import extend_schema
from utils.rbac_permission import SmartRBACPermission
from .models import KnowledgeBase, AIChatHistory, AIChatMessage
from .serializers import KnowledgeBaseSerializer, AIChatHistorySerializer
from .rag_service import RAGService

@extend_schema(tags=["AI 知识库"])
class KnowledgeBaseViewSet(viewsets.ModelViewSet):
    queryset = KnowledgeBase.objects.all()
    serializer_class = KnowledgeBaseSerializer
    permission_classes = [SmartRBACPermission]
    resource_code = "ai:knowledge_base"
    resource_type = "ai"
    resource_owner_field = "creator"

@extend_schema(tags=["AI 对话"])
class AIChatHistoryViewSet(viewsets.ModelViewSet):
    queryset = AIChatHistory.objects.all()
    serializer_class = AIChatHistorySerializer
    permission_classes = [SmartRBACPermission]
    resource_code = "ai:chat"
    resource_type = "ai"
    resource_owner_field = "user_id"

    @action(detail=True, methods=['post'])
    def chat(self, request, pk=None):
        chat_history = self.get_object()
        question = request.data.get('question')
        
        if not question:
            return Response({"error": "Question is required"}, status=status.HTTP_400_BAD_REQUEST)
            
        # Save user message
        AIChatMessage.objects.create(history=chat_history, role='user', content=question)
        
        # Initialize RAG Service
        rag_service = RAGService()
        
        def stream_response():
            full_response = ""
            for chunk in rag_service.chat_stream(question):
                full_response += chunk
                yield chunk
            
            # Save assistant message after stream is done
            # Note: StreamingHttpResponse might need a way to hook into completion, 
            # but for MVP we just save it synchronously or at the end of the generator.
            AIChatMessage.objects.create(history=chat_history, role='assistant', content=full_response)

        return StreamingHttpResponse(stream_response(), content_type='text/event-stream')
