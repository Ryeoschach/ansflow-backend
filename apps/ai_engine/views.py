from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from django.http import StreamingHttpResponse
from drf_spectacular.utils import extend_schema
from utils.rbac_permission import SmartRBACPermission
from .models import KnowledgeBase, AIChatHistory, AIChatMessage, AIProvider, AIModel, AIConfig
from .serializers import (
    KnowledgeBaseSerializer, AIChatHistorySerializer, 
    AIProviderSerializer, AIModelSerializer, AIConfigSerializer
)
from .rag_service import RAGService

@extend_schema(tags=["AI 供应商"])
class AIProviderViewSet(viewsets.ModelViewSet):
    queryset = AIProvider.objects.all()
    serializer_class = AIProviderSerializer
    permission_classes = [SmartRBACPermission]
    resource_code = "ai:provider"
    resource_type = "ai"

    @action(detail=True, methods=['post'])
    def sync_models(self, request, pk=None):
        provider = self.get_object()
        try:
            import requests
            api_key = provider.get_decrypted_key()
            headers = {"Authorization": f"Bearer {api_key}"}
            
            # 适配 OpenAI 兼容的 /models 接口
            url = f"{provider.base_url.rstrip('/')}/models"
            response = requests.get(url, headers=headers, timeout=15)
            
            if response.status_code != 200:
                return Response({
                    "error": f"Failed to fetch models: {response.status_code}",
                    "detail": response.text
                }, status=status.HTTP_400_BAD_REQUEST)

            data = response.json()
            models_list = data.get('data', []) # OpenAI 格式在 data 字段
            
            count = 0
            from django.db import transaction
            with transaction.atomic():
                for m_data in models_list:
                    model_id = m_data.get('id')
                    if not model_id: continue
                    
                    # 简单启发式判断：包含 'embed' 的通常是向量模型
                    m_type = "embedding" if "embed" in model_id.lower() else "llm"
                    
                    # 某些供应商（如 DeepSeek）返回的模型较少，某些（如 OpenAI）返回极多
                    # 我们可以通过一些过滤规则或直接全量同步
                    obj, created = AIModel.objects.update_or_create(
                        provider=provider,
                        name=model_id,
                        defaults={
                            "display_name": model_id, # 初始显示名称设为 ID
                            "model_type": m_type,
                            "is_active": True
                        }
                    )
                    if created: count += 1
            
            return Response({"status": "success", "message": f"Successfully synced {count} new models."})
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

@extend_schema(tags=["AI 模型"])
class AIModelViewSet(viewsets.ModelViewSet):
    queryset = AIModel.objects.all()
    serializer_class = AIModelSerializer
    permission_classes = [SmartRBACPermission]
    resource_code = "ai:model"
    resource_type = "ai"

    def get_queryset(self):
        queryset = super().get_queryset()
        model_type = self.request.query_params.get('model_type')
        if model_type:
            queryset = queryset.filter(model_type=model_type)
        return queryset

@extend_schema(tags=["AI 配置"])
class AIConfigViewSet(viewsets.ModelViewSet):
    queryset = AIConfig.objects.all()
    serializer_class = AIConfigSerializer
    permission_classes = [SmartRBACPermission]
    resource_code = "ai:config"
    resource_type = "ai"

    @action(detail=False, methods=['get'])
    def current(self, request):
        config, _ = AIConfig.objects.get_or_create(name="default")
        serializer = self.get_serializer(config)
        return Response(serializer.data)

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
        llm_id = request.data.get('llm_id')
        embedding_id = request.data.get('embedding_id')
        
        if not question:
            return Response({"error": "Question is required"}, status=status.HTTP_400_BAD_REQUEST)
            
        # Update personality if it changed
        if chat_history.personality != personality:
            chat_history.personality = personality
            chat_history.save(update_fields=['personality'])

        # Save user message
        AIChatMessage.objects.create(history=chat_history, role='user', content=question)
        
        # Initialize RAG Service with model choices
        rag_service = RAGService(
            personality=personality, 
            llm_id=llm_id, 
            embedding_id=embedding_id
        )
        
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
        llm_id = request.data.get('llm_id')
        
        if not prompt_text:
            return Response({"error": "Prompt is required"}, status=status.HTTP_400_BAD_REQUEST)

        # 准备动态上下文...
        from apps.task_management.models import AnsibleTask
        from apps.k8s_management.models import K8sCluster
        
        context_data = {
            'ansible_tasks': list(AnsibleTask.objects.all().values('id', 'name')),
            'k8s_clusters': list(K8sCluster.objects.all().values('id', 'name')),
        }

        rag_service = RAGService(personality=personality, llm_id=llm_id)
        try:
            dag_json_str = rag_service.generate_dag(prompt_text, context_data=context_data)
            # ... (保持原有的 JSON 清洗逻辑)
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

    # ... (保持 save_to_knowledge 不变)

    @action(detail=True, methods=['post'], url_path='diagnose')
    def diagnose(self, request, pk=None):
        target_type = request.data.get('target_type')
        target_id = request.data.get('target_id')
        history_id = pk # 详情页中的 PK 即 history_id
        personality = request.data.get('personality', 'professional')
        llm_id = request.data.get('llm_id')
        embedding_id = request.data.get('embedding_id')
        
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
