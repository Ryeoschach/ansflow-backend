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
