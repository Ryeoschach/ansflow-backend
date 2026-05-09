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
    queryset = AIChatHistory.objects.all().order_by('-update_time')
    serializer_class = AIChatHistorySerializer
    permission_classes = [SmartRBACPermission]
    resource_code = "ai:chat"
    resource_type = "ai"
    resource_owner_field = "user_id"

    def get_queryset(self):
        queryset = super().get_queryset()
        history_type = self.request.query_params.get('history_type')
        search = self.request.query_params.get('search')
        
        if history_type:
            queryset = queryset.filter(history_type=history_type)
        if search:
            queryset = queryset.filter(title__icontains=search)
            
        return queryset

    @action(detail=True, methods=['get'])
    def messages(self, request, pk=None):
        chat_history = self.get_object()
        messages = chat_history.messages.all().order_by('create_time')
        from .serializers import AIChatMessageSerializer
        serializer = AIChatMessageSerializer(messages, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['post'])
    def chat(self, request, pk=None):
        chat_history = self.get_object()
        question = request.data.get('question')
        personality = request.data.get('personality', 'professional')
        
        if not question:
            return Response({"error": "Question is required"}, status=status.HTTP_400_BAD_REQUEST)
            
        # Update personality if it changed
        if chat_history.personality != personality:
            chat_history.personality = personality
            chat_history.save(update_fields=['personality'])

        # Save user message
        AIChatMessage.objects.create(history=chat_history, role='user', content=question)
        
        # Initialize RAG Service with personality
        rag_service = RAGService(personality=personality)
        
        def stream_response():
            full_response = ""
            for chunk in rag_service.chat_stream(question, history_id=pk):
                full_response += chunk
                yield chunk
            
            # Save assistant message after stream is done
            msg = AIChatMessage.objects.create(history=chat_history, role='assistant', content=full_response)
            yield f"\n__MESSAGE_ID__:{msg.id}"

        return StreamingHttpResponse(stream_response(), content_type='text/event-stream')

    @action(detail=False, methods=['post'], url_path='generate-pipeline')
    def generate_pipeline(self, request):
        prompt_text = request.data.get('prompt')
        personality = request.data.get('personality', 'professional')
        if not prompt_text:
            return Response({"error": "Prompt is required"}, status=status.HTTP_400_BAD_REQUEST)

        # 准备动态上下文：获取当前的 Ansible 任务和 K8s 集群
        from apps.task_management.models import AnsibleTask
        from apps.k8s_management.models import K8sCluster
        
        context_data = {
            'ansible_tasks': list(AnsibleTask.objects.all().values('id', 'name')),
            'k8s_clusters': list(K8sCluster.objects.all().values('id', 'name')),
        }

        rag_service = RAGService(personality=personality)
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

    @action(detail=True, methods=['post'], url_path='save-to-knowledge')
    def save_to_knowledge(self, request, pk=None):
        message_id = request.data.get('message_id')
        if not message_id:
            return Response({"error": "message_id is required"}, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            msg = AIChatMessage.objects.get(id=message_id, history_id=pk)
            if msg.role != 'assistant':
                return Response({"error": "Only assistant messages can be saved to knowledge base"}, status=status.HTTP_400_BAD_REQUEST)
            
            # 获取上下文（上一个用户问题）
            prev_msg = AIChatMessage.objects.filter(
                history_id=pk, 
                create_time__lt=msg.create_time, 
                role='user'
            ).order_by('-create_time').first()
            
            question = prev_msg.content if prev_msg else "Unknown Question"
            knowledge_content = f"问题: {question}\n答案: {msg.content}"
            
            rag_service = RAGService()
            rag_service.add_knowledge(
                content=knowledge_content, 
                metadata={
                    "source": "chat_history", 
                    "history_id": pk, 
                    "message_id": message_id,
                    "type": "human_verified_knowledge"
                }
            )
            # 标记为已导出
            msg.is_exported = True
            msg.save(update_fields=['is_exported'])
            
            return Response({"message": "Successfully saved to knowledge base"}, status=status.HTTP_200_OK)
        except AIChatMessage.DoesNotExist:
            return Response({"error": "Message not found"}, status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=False, methods=['post'])
    def diagnose(self, request):
        target_type = request.data.get('target_type') # 'pipeline' or 'task'
        target_id = request.data.get('target_id')
        history_id = request.data.get('history_id')
        personality = request.data.get('personality', 'professional')
        
        if not target_type or not target_id:
            return Response({"error": "target_type and target_id are required"}, status=status.HTTP_400_BAD_REQUEST)

        log_content = ""
        context_info = {"type": target_type, "id": target_id}
        target_name_display = f"ID: {target_id}"

        # Get history object if provided
        chat_history = None
        if history_id:
            try:
                chat_history = AIChatHistory.objects.get(id=history_id)
            except AIChatHistory.DoesNotExist:
                pass

        try:
            if target_type == 'pipeline':
                from apps.pipeline_management.models import PipelineNodeRun, PipelineRun
                run = PipelineRun.objects.filter(id=target_id).first()
                if run:
                    target_name_display = f"流水线: {run.pipeline.name} (运行实例: #{target_id})"
                
                node_run = PipelineNodeRun.objects.filter(run_id=target_id, status='failed').last()
                if node_run:
                    log_content = node_run.logs or "No logs found"
                    context_info["name"] = f"Pipeline Node: {node_run.node_label}"
                    context_info["summary"] = f"Node {node_run.node_id} failed"
                else:
                    return Response({"error": "No failed node found for this pipeline run"}, status=status.HTTP_404_NOT_FOUND)
            elif target_type == 'task':
                from apps.task_management.models import TaskLog, AnsibleExecution
                exec_obj = AnsibleExecution.objects.filter(id=target_id).first()
                if exec_obj:
                    target_name_display = f"任务: {exec_obj.task.name} (执行实例: #{target_id})"

                logs = TaskLog.objects.filter(execution_id=target_id).order_by('create_time')
                log_content = "\n".join([f"[{log.host}] {log.output}" for log in logs])
                context_info["name"] = f"Ansible Task Execution {target_id}"
                context_info["summary"] = "Task execution failed"
            else:
                return Response({"error": "Invalid target_type"}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({"error": f"Failed to fetch logs: {str(e)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        # Save the user request to history if we have one
        if chat_history:
            # Update personality if needed
            if chat_history.personality != personality:
                chat_history.personality = personality
                chat_history.save(update_fields=['personality'])
            
            # Record the prompt as a user message
            prompt_content = f"请帮我诊断【{target_name_display}】的错误原因。"
            AIChatMessage.objects.create(history=chat_history, role='user', content=prompt_content)

        rag_service = RAGService(personality=personality)
        
        suggested_pipeline_id = None
        from apps.sre_management.models import SelfHealingPolicy
        policies = SelfHealingPolicy.objects.filter(is_active=True)
        for policy in policies:
            for key, value in policy.alert_match_rule.items():
                if value.lower() in log_content.lower():
                    suggested_pipeline_id = policy.pipeline_id
                    break
            if suggested_pipeline_id: break

        def stream_diagnosis():
            print(f"[AI] Starting diagnosis for {target_type} {target_id}")
            full_response = ""
            if suggested_pipeline_id:
                import json
                yield f"__SUGGESTION__:{json.dumps({'pipeline_id': suggested_pipeline_id})}\n"
            
            try:
                print("[AI] Calling rag_service.diagnose_log...")
                for chunk in rag_service.diagnose_log(log_content, context_info):
                    full_response += chunk
                    yield chunk
                print("[AI] Diagnosis stream completed")
            except Exception as e:
                print(f"[AI] Error during diagnosis stream: {str(e)}")
                yield f"\n\n❌ 诊断过程发生错误: {str(e)}"
            
            # Save assistant response to history
            if chat_history and full_response:
                msg = AIChatMessage.objects.create(history=chat_history, role='assistant', content=full_response)
                print(f"[AI] Response saved to history, ID: {msg.id}")
                yield f"\n__MESSAGE_ID__:{msg.id}"

        return StreamingHttpResponse(stream_diagnosis(), content_type='text/event-stream')
