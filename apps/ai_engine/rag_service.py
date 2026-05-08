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
from django.conf import settings

class RAGService:
    def __init__(self, collection_name: str = "ansflow_docs"):
        self.collection_name = collection_name
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

    def format_docs(self, docs):
        return "\n\n".join(doc.page_content for doc in docs)

    def get_chat_chain(self):
        retriever = self.vectorstore.as_retriever(search_kwargs={"k": 3})
        template = """你是一个AnsFlow DevOps平台的智能运维助手。
请使用以下检索到的参考内容来回答用户的问题。如果你不知道答案，就明确说明你不知道，不要编造。

参考内容：
{context}

用户问题：{question}

你的回答："""
        prompt = ChatPromptTemplate.from_template(template)
        chain = (
            {"context": retriever | self.format_docs, "question": RunnablePassthrough()}
            | prompt
            | self.llm
            | StrOutputParser()
        )
        return chain

    def chat_stream(self, question: str):
        chain = self.get_chat_chain()
        for chunk in chain.stream(question):
            yield chunk

    def diagnose_log(self, log_content: str, context_info: dict):
        retriever = self.vectorstore.as_retriever(search_kwargs={"k": 3})
        template = """你是一个专业的 SRE 运维专家。请分析以下AnsFlow平台的执行日志，并结合参考内容给出诊断结论和修复建议。

【执行上下文】
- 类型: {target_type}
- 名称: {target_name}
- 错误摘要: {error_summary}

【错误日志截取】
{log_content}

【参考知识库】
{context}

请按以下格式回答：
### 🔍 故障根因
(描述为什么报错)

### 🛠️ 修复建议
(给出具体的步骤或代码修改建议)

### 💡 预防措施
(如何避免下次发生)
"""
        prompt = ChatPromptTemplate.from_template(template)
        chain = (
            {"context": (lambda x: x["log_content"]) | retriever | self.format_docs, 
             "question": lambda x: x["log_content"],
             "target_type": lambda x: x["target_type"],
             "target_name": lambda x: x["target_name"],
             "error_summary": lambda x: x["error_summary"],
             "log_content": lambda x: x["log_content"]}
            | prompt
            | self.llm
            | StrOutputParser()
        )
        return chain.stream({
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
5. 边(edges)的 id 格式为 'e-source-target'。

用户需求：{prompt_text}

JSON 输出："""
        prompt = ChatPromptTemplate.from_template(template)
        chain = prompt | self.llm | StrOutputParser()
        return chain.invoke({
            "prompt_text": prompt_text,
            "dynamic_context": context_str
        })
