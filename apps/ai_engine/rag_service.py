import os
from typing import List, Optional
from langchain_community.document_loaders import TextLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_community.embeddings.fastembed import FastEmbedEmbeddings
from langchain_chroma import Chroma
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_core.runnables import RunnablePassthrough
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from django.conf import settings

class RAGService:
    PERSONALITIES = {
        'professional': {
            'name': '技术专家',
            'desc': '严谨、专业，提供深度技术细节。',
            'prefix': '你是一个资深的 AnsFlow SRE 专家。你的回答应该专业、客观，包含必要的技术细节和代码示例。'
        },
        'concise': {
            'name': '简洁助手',
            'desc': '高效、直白，只说干货。',
            'prefix': '你是一个高效的运维助手。请用最简短的语言回答问题，直接给出结论和命令，避免废话。'
        },
        'humorous': {
            'name': '幽默特工',
            'desc': '风趣、亲切，缓解运维压力。',
            'prefix': '你是一个热爱生活的运维老工。虽然运维很苦，但你的回答总能带着一点点幽默感，偶尔调侃一下 Bug，让用户放轻松，但也要解决问题。'
        }
    }

    def __init__(self, collection_name: str = "ansflow_docs", personality: str = 'professional'):
        self.collection_name = collection_name
        self.personality = self.PERSONALITIES.get(personality, self.PERSONALITIES['professional'])
        self.persist_directory = os.path.join(settings.BASE_DIR, "chroma_db")
        self.cache_directory = os.path.join(settings.BASE_DIR, ".model_cache")
        
        if not os.path.exists(self.cache_directory):
            os.makedirs(self.cache_directory)

        self.embeddings = FastEmbedEmbeddings(
            model_name="BAAI/bge-small-en-v1.5",
            cache_dir=self.cache_directory
        )
        
        self.vectorstore = Chroma(
            collection_name=self.collection_name,
            embedding_function=self.embeddings,
            persist_directory=self.persist_directory
        )
        
        self.api_key = os.environ.get("LLM_API_KEY")
        self.api_base = os.environ.get("LLM_API_BASE", "https://api.deepseek.com")
        
        self.llm = ChatOpenAI(
            model="deepseek-chat", 
            api_key=self.api_key, 
            base_url=self.api_base,
            streaming=True
        )

    def ingest_document(self, file_path: str):
        loader = TextLoader(file_path)
        docs = loader.load()
        text_splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=200)
        splits = text_splitter.split_documents(docs)
        self.vectorstore.add_documents(documents=splits)
        return len(splits)

    def add_knowledge(self, content: str, metadata: dict = None):
        """Manually add a piece of knowledge to the vector store."""
        from langchain_core.documents import Document
        doc = Document(page_content=content, metadata=metadata or {})
        self.vectorstore.add_documents(documents=[doc])
        return True

    def format_docs(self, docs):
        return "\n\n".join(doc.page_content for doc in docs)

    def get_chat_chain(self, history_id: int = None):
        """Returns a LangChain runnable chain for chatting with memory support."""
        retriever = self.vectorstore.as_retriever(search_kwargs={"k": 3})
        
        # 加载历史消息作为上下文 (取最近 20 条)
        chat_memory_str = ""
        if history_id:
            from .models import AIChatMessage
            prev_messages = AIChatMessage.objects.filter(history_id=history_id).order_by('create_time')[:20]
            for m in prev_messages:
                role = "用户" if m.role == 'user' else "助手"
                chat_memory_str += f"{role}: {m.content}\n"

        template = f"""{self.personality['prefix']}
请使用以下检索到的参考内容来回答用户的问题。如果你不知道答案，就明确说明你不知道，不要编造。

参考内容：
{{context}}

对话历史：
{chat_memory_str}

用户问题：{{question}}

你的回答："""
        prompt = ChatPromptTemplate.from_template(template)
        
        chain = (
            {"context": retriever | self.format_docs, "question": RunnablePassthrough()}
            | prompt
            | self.llm
            | StrOutputParser()
        )
        return chain

    def chat_stream(self, question: str, history_id: int = None):
        chain = self.get_chat_chain(history_id=history_id)
        for chunk in chain.stream(question):
            yield chunk

    def diagnose_log(self, log_content: str, context_info: dict):
        # 1. 语义缓存：尝试寻找高相似度的已验证知识
        try:
            # 使用更基础的接口获取分数 (分数越低越相似，Chroma 默认是 L2 距离)
            results = self.vectorstore.similarity_search_with_score(
                log_content, 
                k=1, 
                filter={"type": "human_verified_knowledge"}
            )
            
            if results:
                doc, distance = results[0]
                # L2 距离越接近 0 表示越相似。通常距离 < 0.2 可以认为非常相似
                if distance < 0.15:
                    yield "✨ **已为您匹配到历史最佳解决方案 (语义缓存)**\n\n"
                    answer = doc.page_content.split("答案: ")[-1]
                    yield answer
                    return
        except Exception as e:
            print(f"[RAG] Semantic cache search failed: {e}")

        # 2. 如果没有命中缓存，走常规 RAG + LLM 流程
        retriever = self.vectorstore.as_retriever(search_kwargs={"k": 3})
        
        from langchain_core.runnables import RunnableLambda
        from langchain_core.prompts import ChatPromptTemplate

        template = f"""{self.personality['prefix']}
作为专业的 SRE 运维专家，请分析以下执行日志并给出诊断结论和修复建议。

【执行上下文】
- 类型: {{target_type}}
- 名称: {{target_name}}
- 错误摘要: {{error_summary}}

【错误日志截取】
{{log_content}}

【参考知识库】
{{context}}

请按以下格式回答：
### 🔍 故障根因
(描述为什么报错)

### 🛠️ 修复建议
(给出具体的步骤或代码修改建议)

### 💡 预防措施
(如何避免下次发生)
"""
        prompt = ChatPromptTemplate.from_template(template)
        
        # 使用 RunnableLambda 包裹普通函数
        get_context = RunnableLambda(lambda x: retriever.invoke(x["log_content"])) | self.format_docs

        chain = (
            {"context": get_context, 
             "target_type": lambda x: x["target_type"],
             "target_name": lambda x: x["target_name"],
             "error_summary": lambda x: x["error_summary"],
             "log_content": lambda x: x["log_content"]}
            | prompt
            | self.llm
            | StrOutputParser()
        )
        
        yield from chain.stream({
            "log_content": log_content,
            "target_type": context_info.get("type", "Unknown"),
            "target_name": context_info.get("name", "Unknown"),
            "error_summary": context_info.get("summary", "Execution failed")
        })

    def generate_dag(self, prompt_text: str, context_data: dict = None):
        """Generates a ReactFlow compatible DAG structure based on user prompt."""
        context_str = ""
        if context_data:
            if 'ansible_tasks' in context_data:
                context_str += "\n【可用 Ansible 任务列表】\n"
                for t in context_data['ansible_tasks']:
                    context_str += f"- ID: {t['id']}, 名称: {t['name']}\n"
            
            if 'k8s_clusters' in context_data:
                context_str += "\n【可用 K8s 集群列表】\n"
                for c in context_data['k8s_clusters']:
                    context_str += f"- ID: {c['id']}, 名称: {c['name']}\n"

        template = """你是一个专业的 AnsFlow 流水线设计专家。
请根据用户的需求，生成一个符合 ReactFlow 规范的 JSON 格式 DAG 流水线数据。
{dynamic_context}
【节点类型规范】
- ansible: 执行 Ansible 任务。参数: {{'ansible_task_id': int, 'label': string, 'max_retries': int, 'retry_delay': int}}
- k8s_deploy: 部署 K8s 资源。参数: {{'cluster_id': int, 'manifest': string, 'label': string}}
- git_clone: 克隆代码。参数: {{'repo_url': string, 'branch': string, 'label': string}}
- docker_build: 构建镜像。参数: {{'image_name': string, 'dockerfile': string, 'label': string}}

【输出要求】
1. 只输出纯 JSON 格式，不要包含 Markdown 标记或任何解释。
2. JSON 结构必须包含 'nodes' 和 'edges'。
3. 如果用户提到的任务名称或集群名称在【可用列表】中存在，请务必使用对应的正确 ID。
4. 如果用户指定了尝试次数或间隔时间，请准确填写到对应参数中。
5. 节点位置(position)请合理计算，使其水平从左向右排列，间距 300px，y 轴设为 100。
6. 边(edges)的 id 格式为 'e-source-target'。

用户需求：{prompt_text}

JSON 输出："""
        prompt = ChatPromptTemplate.from_template(template)
        chain = prompt | self.llm | StrOutputParser()
        return chain.invoke({
            "prompt_text": prompt_text,
            "dynamic_context": context_str
        })
