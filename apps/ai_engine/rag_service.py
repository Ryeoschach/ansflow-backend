import os
from typing import List, Optional
from langchain_community.document_loaders import TextLoader, UnstructuredMarkdownLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_huggingface import HuggingFaceEmbeddings
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
        
        # Use local embeddings to save cost and avoid needing an embedding API key
        self.embeddings = HuggingFaceEmbeddings(model_name="sentence-transformers/all-MiniLM-L6-v2")
        
        self.vectorstore = Chroma(
            collection_name=self.collection_name,
            embedding_function=self.embeddings,
            persist_directory=self.persist_directory
        )
        
        # Load API key and Base URL from environment
        self.api_key = os.environ.get("LLM_API_KEY")
        self.api_base = os.environ.get("LLM_API_BASE", "https://api.deepseek.com")
        
        # We use ChatOpenAI because DeepSeek API is fully compatible with OpenAI SDK
        self.llm = ChatOpenAI(
            model="deepseek-chat", 
            api_key=self.api_key, 
            base_url=self.api_base,
            streaming=True
        )

    def ingest_document(self, file_path: str):
        """Loads a document, splits it, and adds it to the vector store."""
        if file_path.endswith('.md'):
            # Simple TextLoader can handle MD too, and it's less error-prone than unstructured
            loader = TextLoader(file_path)
        else:
            loader = TextLoader(file_path)
            
        docs = loader.load()
        text_splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=200)
        splits = text_splitter.split_documents(docs)
        
        self.vectorstore.add_documents(documents=splits)
        return len(splits)

    def format_docs(self, docs):
        return "\n\n".join(doc.page_content for doc in docs)

    def get_chat_chain(self):
        """Returns a LangChain runnable chain for chatting."""
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
        """Generates a streaming response for the given question."""
        chain = self.get_chat_chain()
        for chunk in chain.stream(question):
            yield chunk

    def diagnose_log(self, log_content: str, context_info: dict):
        """Specially tailored for log diagnosis."""
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
        
        # Build the chain
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
