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
            AIChatMessage.objects.create(history=chat_history, role='assistant', content=full_response)

        return StreamingHttpResponse(stream_response(), content_type='text/event-stream')

    @action(detail=False, methods=['post'], url_path='generate-pipeline')
    def generate_pipeline(self, request):
        prompt_text = request.data.get('prompt')
        if not prompt_text:
            return Response({"error": "Prompt is required"}, status=status.HTTP_400_BAD_REQUEST)

        # 准备动态上下文：获取当前的 Ansible 任务和 K8s 集群
        from apps.task_management.models import AnsibleTask
        from apps.k8s_management.models import K8sCluster
        
        context_data = {
            'ansible_tasks': list(AnsibleTask.objects.all().values('id', 'name')),
            'k8s_clusters': list(K8sCluster.objects.all().values('id', 'name')),
        }

        rag_service = RAGService()
        try:
            dag_json_str = rag_service.generate_dag(prompt_text, context_data=context_data)
            # LLM might return JSON wrapped in backticks, clean it
            clean_json = dag_json_str.strip()
            if clean_json.startswith("```json"):
                clean_json = clean_json[7:]
            if clean_json.endswith("```"):
                clean_json = clean_json[:-3]
            
            import json
            dag_data = json.loads(clean_json.strip())
            return Response(dag_data, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({"error": f"Failed to generate pipeline: {str(e)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=False, methods=['post'])
    def diagnose(self, request):
        target_type = request.data.get('target_type') # 'pipeline' or 'task'
        target_id = request.data.get('target_id')
        
        if not target_type or not target_id:
            return Response({"error": "target_type and target_id are required"}, status=status.HTTP_400_BAD_REQUEST)

        log_content = ""
        context_info = {"type": target_type, "id": target_id}

        try:
            if target_type == 'pipeline':
                from apps.pipeline_management.models import PipelineNodeRun
                # Get the latest failed node run for this pipeline run instance
                node_run = PipelineNodeRun.objects.filter(run_id=target_id, status='failed').last()
                if node_run:
                    log_content = node_run.logs or "No logs found"
                    context_info["name"] = f"Pipeline Node: {node_run.node_label}"
                    context_info["summary"] = f"Node {node_run.node_id} failed"
                else:
                    return Response({"error": "No failed node found for this pipeline run"}, status=status.HTTP_404_NOT_FOUND)
            
            elif target_type == 'task':
                from apps.task_management.models import TaskLog
                logs = TaskLog.objects.filter(execution_id=target_id).order_by('create_time')
                log_content = "\n".join([f"[{log.host}] {log.output}" for log in logs])
                context_info["name"] = f"Ansible Task Execution {target_id}"
                context_info["summary"] = "Task execution failed"
            
            else:
                return Response({"error": "Invalid target_type"}, status=status.HTTP_400_BAD_REQUEST)

        except Exception as e:
            return Response({"error": f"Failed to fetch logs: {str(e)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        # Initialize RAG Service for diagnosis
        rag_service = RAGService()
        
        def stream_diagnosis():
            for chunk in rag_service.diagnose_log(log_content, context_info):
                yield chunk

        return StreamingHttpResponse(stream_diagnosis(), content_type='text/event-stream')
