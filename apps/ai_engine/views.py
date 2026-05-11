from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from django.http import StreamingHttpResponse
from drf_spectacular.utils import extend_schema
from utils.rbac_permission import SmartRBACPermission, DataScopeMixin
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
            from requests.exceptions import RequestException, JSONDecodeError
            
            api_key = provider.get_decrypted_key()
            headers = {}
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            
            base_url = provider.base_url.rstrip('/') if provider.base_url else ""
            
            # 根据供应商类型选择同步接口
            if provider.provider_type == 'local':
                # 动态扫描本地模型缓存目录
                from django.conf import settings
                import os
                
                cache_dir = os.path.join(settings.BASE_DIR, ".model_cache")
                models_list = []
                
                if os.path.exists(cache_dir):
                    for item in os.listdir(cache_dir):
                        # FastEmbed 目录名规范: models--qdrant--bge-small-en-v1.5-onnx-q
                        if item.startswith("models--"):
                            # 还原模型标识 (这里做一个简单的映射，通常 BGE 系列最常用)
                            # 也可以直接存目录名的一部分，FastEmbed 加载时能识别
                            m_id = ""
                            if "bge-small-en" in item: m_id = "BAAI/bge-small-en-v1.5"
                            elif "bge-small-zh" in item: m_id = "BAAI/bge-small-zh-v1.5"
                            else:
                                # 回退：尝试从目录名推测
                                parts = item.split("--")
                                if len(parts) >= 3:
                                    m_id = f"{parts[1]}/{parts[2].replace('-onnx-q', '')}"
                            
                            if m_id:
                                models_list.append({"id": m_id, "type": "embedding"})

                # 如果没扫描到，至少返回一个默认配置中定义的
                if not models_list:
                    models_list = [{"id": "BAAI/bge-small-en-v1.5", "type": "embedding"}]

                count = 0
                from django.db import transaction
                with transaction.atomic():
                    for m_data in models_list:
                        obj, created = AIModel.objects.update_or_create(
                            provider=provider,
                            name=m_data['id'],
                            defaults={
                                "display_name": m_data['id'].split('/')[-1] + " (Local)",
                                "model_type": m_data['type'],
                                "is_active": True
                            }
                        )
                        if created: count += 1
                return Response({"status": "success", "message": f"Successfully detected {count} local models."})

            if not base_url:
                return Response({"error": "Base URL is not configured"}, status=status.HTTP_400_BAD_REQUEST)
                
            if provider.provider_type == 'ollama':
                url = f"{base_url}/api/tags"
            else:
                url = f"{base_url}/models"

            try:
                response = requests.get(url, headers=headers, timeout=15)
                
                if response.status_code != 200:
                    return Response({
                        "error": f"API returned error status: {response.status_code}",
                        "detail": response.text[:500]
                    }, status=status.HTTP_400_BAD_REQUEST)

                try:
                    data = response.json()
                except (ValueError, JSONDecodeError):
                    return Response({
                        "error": "Failed to parse API response as JSON",
                        "detail": response.text[:500]
                    }, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

                # 适配多种返回格式: OpenAI (data), Ollama (models), 或者直接是列表
                models_list = []
                if isinstance(data, dict):
                    if 'data' in data: # OpenAI
                        models_list = data['data']
                    elif 'models' in data: # Ollama
                        models_list = data['models']
                elif isinstance(data, list):
                    models_list = data

                if not models_list:
                    return Response({"status": "success", "message": "No models found in provider."})

                count = 0
                from django.db import transaction
                with transaction.atomic():
                    for m_data in models_list:
                        if not isinstance(m_data, dict): continue
                        # OpenAI 使用 'id', Ollama 使用 'name'
                        model_id = m_data.get('id') or m_data.get('name')
                        if not model_id: continue
                        
                        # 简单启发式判断：包含 'embed' 的通常是向量模型
                        m_type = "embedding" if "embed" in model_id.lower() else "llm"
                        
                        obj, created = AIModel.objects.update_or_create(
                            provider=provider,
                            name=model_id,
                            defaults={
                                "display_name": model_id,
                                "model_type": m_type,
                                "is_active": True
                            }
                        )
                        if created: count += 1
                
                return Response({"status": "success", "message": f"Successfully synced {count} new models."})
                
            except RequestException as e:
                return Response({"error": f"Network error connecting to provider: {str(e)}"}, status=status.HTTP_502_BAD_GATEWAY)

        except Exception as e:
            return Response({"error": f"Internal server error: {str(e)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

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
class AIChatHistoryViewSet(DataScopeMixin, viewsets.ModelViewSet):
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
        
        # 获取权限感知上下文，即便在普通对话中也能进行精准编排
        from .utils import get_authorized_resources
        auth_context = get_authorized_resources(request.user)

        # Initialize RAG Service with model choices
        rag_service = RAGService(
            personality=personality, 
            llm_id=llm_id, 
            embedding_id=embedding_id
        )
        
        def stream_response():
            full_response = ""
            for chunk in rag_service.chat_stream(question, history_id=pk, auth_context=auth_context):
                full_response += chunk
                yield chunk
            
            # Save assistant message after stream is done
            msg = AIChatMessage.objects.create(history=chat_history, role='assistant', content=full_response)
            yield f"\n__MESSAGE_ID__:{msg.id}"

        return StreamingHttpResponse(stream_response(), content_type='text/event-stream')

    @action(detail=False, methods=['post'], url_path='generate-pipeline')
    def generate_pipeline(self, request):
        prompt = request.data.get('prompt')
        llm_id = request.data.get('llm_id')

        if not prompt:
            return Response({"error": "Prompt is required"}, status=status.HTTP_400_BAD_REQUEST)

        # 获取权限感知上下文
        from .utils import get_authorized_resources
        auth_context = get_authorized_resources(request.user)

        rag_service = RAGService(llm_id=llm_id)
        try:
            suggested_json_str = rag_service.generate_dag(prompt, context_data=auth_context)
            
            clean_json = suggested_json_str.strip()
            if clean_json.startswith("```json"):
                clean_json = clean_json[7:]
            if clean_json.endswith("```"):
                clean_json = clean_json[:-3]
            
            import json
            suggested_data = json.loads(clean_json.strip())
            return Response(suggested_data, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({"error": f"Failed to generate pipeline: {str(e)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=False, methods=['post'], url_path='refine-pipeline')
    def refine_pipeline(self, request):
        prompt = request.data.get('prompt')
        nodes = request.data.get('nodes', [])
        edges = request.data.get('edges', [])
        llm_id = request.data.get('llm_id')

        if not prompt:
            return Response({"error": "Prompt is required"}, status=status.HTTP_400_BAD_REQUEST)

        # 获取权限感知上下文
        from .utils import get_authorized_resources
        auth_context = get_authorized_resources(request.user)

        rag_service = RAGService(llm_id=llm_id)
        try:
            suggested_json_str = rag_service.refine_dag(prompt, nodes, edges, auth_context=auth_context)
            
            clean_json = suggested_json_str.strip()
            if clean_json.startswith("```json"):
                clean_json = clean_json[7:]
            if clean_json.endswith("```"):
                clean_json = clean_json[:-3]
            
            import json
            suggested_data = json.loads(clean_json.strip())
            return Response(suggested_data, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({"error": f"Failed to refine pipeline: {str(e)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=False, methods=['post'], url_path='explain-pipeline')
    def explain_pipeline(self, request):
        nodes = request.data.get('nodes', [])
        edges = request.data.get('edges', [])
        llm_id = request.data.get('llm_id')

        if not nodes:
            return Response({"error": "Pipeline nodes are required"}, status=status.HTTP_400_BAD_REQUEST)

        rag_service = RAGService(llm_id=llm_id)
        try:
            explanation = rag_service.explain_pipeline(nodes, edges)
            return Response({"explanation": explanation}, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({"error": f"Failed to explain pipeline: {str(e)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=False, methods=['post'], url_path='suggest-node-params')
    def suggest_node_params(self, request):
        node_type = request.data.get('type')
        current_data = request.data.get('data', {})
        context = request.data.get('context', [])
        llm_id = request.data.get('llm_id')

        if not node_type:
            return Response({"error": "Node type is required"}, status=status.HTTP_400_BAD_REQUEST)

        rag_service = RAGService(llm_id=llm_id)
        try:
            suggested_json_str = rag_service.suggest_node_params(
                node_type, 
                current_data=current_data, 
                pipeline_context=context
            )
            
            clean_json = suggested_json_str.strip()
            if clean_json.startswith("```json"):
                clean_json = clean_json[7:]
            if clean_json.endswith("```"):
                clean_json = clean_json[:-3]
            
            import json
            suggested_data = json.loads(clean_json.strip())
            return Response(suggested_data, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({"error": f"Failed to suggest params: {str(e)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=['post'], url_path='save-to-knowledge')
    def save_to_knowledge(self, request, pk=None):
        chat_history = self.get_object()
        message_id = request.data.get('message_id')
        
        try:
            msg = AIChatMessage.objects.get(id=message_id, history=chat_history)
            
            # 调用 RAG 服务存入向量库
            rag_service = RAGService()
            success = rag_service.add_knowledge(
                content=msg.content,
                metadata={
                    "source": f"chat_history_{pk}",
                    "type": "human_verified_knowledge",
                    "user": request.user.username
                }
            )
            
            if success:
                msg.is_exported = True
                msg.save()
                return Response({"status": "success"})
            return Response({"error": "Failed to add to vector store"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        except AIChatMessage.DoesNotExist:
            return Response({"error": "Message not found"}, status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

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
            elif target_type == 'alert':
                from apps.sre_management.models import AlertEvent
                alert = AlertEvent.objects.filter(id=target_id).first()
                if not alert:
                    return Response({"error": "Alert not found"}, status=status.HTTP_404_NOT_FOUND)
                
                target_name_display = f"告警: {alert.alert_name} (ID: #{target_id})"
                
                # 整合告警上下文
                log_content = f"告警详情:\n名称: {alert.alert_name}\n严重程度: {alert.severity}\n状态: {alert.status}\n标签: {alert.labels}\n注释: {alert.annotations}"
                if alert.ai_analysis:
                    log_content += f"\n\n初步分析建议:\n{alert.ai_analysis}"
                
                context_info["name"] = f"SRE Alert: {alert.alert_name}"
                context_info["summary"] = f"Alert triggered with severity {alert.severity}"
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

        # 获取权限感知上下文，辅助 AI 给出可执行的建议
        from .utils import get_authorized_resources
        auth_context = get_authorized_resources(request.user)

        rag_service = RAGService(personality=personality)
        
        suggested_pipeline_id = None
        from apps.sre_management.models import SelfHealingPolicy
        policies = SelfHealingPolicy.objects.filter(is_active=True)
        # TODO: 这里也需要考虑数据权限过滤，暂时先匹配规则
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
                for chunk in rag_service.diagnose_log(log_content, context_info, auth_context=auth_context):
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
