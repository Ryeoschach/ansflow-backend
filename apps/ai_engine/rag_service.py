import os
from typing import List, Optional
from langchain_community.document_loaders import TextLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
# 移除顶部的 FastEmbedEmbeddings 导入
from langchain_chroma import Chroma
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_core.runnables import RunnablePassthrough
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from django.conf import settings
from langchain_community.retrievers import BM25Retriever
# 移除顶部的 FlashrankRerank 导入
import jieba

def chinese_tokenizer(text: str):
    """使用 jieba 进行中文分词，提高 BM25 的匹配精度"""
    # lcut_for_search 适合用于搜索引擎，会对长词进一步切分
    return jieba.lcut_for_search(text.lower())

try:
    from langchain.retrievers import EnsembleRetriever
    from langchain.retrievers.contextual_compression import ContextualCompressionRetriever
except ImportError:
    from langchain_classic.retrievers import EnsembleRetriever
    from langchain_classic.retrievers.contextual_compression import ContextualCompressionRetriever

from .models import AIConfig, AIModel, AIProvider, KnowledgeChunk, KnowledgeDocument, KnowledgeBase

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

    _vectorstore_cache = {}
    _embeddings_cache = {}
    _reranker_cache = None

    def __init__(self, collection_name: str = "ansflow_docs", personality: str = 'professional',
                 llm_id: int = None, embedding_id: int = None):
        self.personality = self.PERSONALITIES.get(personality, self.PERSONALITIES['professional'])
        self.persist_directory = os.path.join(settings.BASE_DIR, "chroma_db")
        self.cache_directory = os.path.join(settings.BASE_DIR, ".model_cache")
        
        if not os.path.exists(self.cache_directory):
            os.makedirs(self.cache_directory)

        # 1. 初始化配置
        self.config = AIConfig.objects.filter(name="default").first()
        self.llm_config = self._get_model_config(llm_id, "llm")
        self.emb_config = self._get_model_config(embedding_id, "embedding")
        self.rerank_config = self._get_model_config(None, "rerank")

        # 2. 初始化 Embeddings (带缓存)
        emb_key = f"{self.emb_config['name']}_{self.emb_config['provider_type']}"
        if emb_key not in self._embeddings_cache:
            self._embeddings_cache[emb_key] = self._init_embeddings()
        self.embeddings = self._embeddings_cache[emb_key]

        # 3. 初始化 Reranker (带缓存)
        if RAGService._reranker_cache is None:
            rerank_ptype = self.rerank_config.get('provider_type')
            reranker_base = self.rerank_config.get('base_url')
            reranker_model = self.rerank_config.get('name')

            # 优先尝试远程 Reranker (如 Xinference)
            if rerank_ptype != "local" and reranker_base:
                try:
                    from langchain_community.document_compressors import XinferenceRerank
                    # 纠错：XinferenceRerank 内部通常会自动拼路径，如果用户填了 /v1 则去掉它
                    clean_rerank_base = reranker_base.rstrip('/').replace("/v1", "")
                    RAGService._reranker_cache = XinferenceRerank(
                        base_url=clean_rerank_base,
                        model_name=reranker_model
                    )
                    print(f"[RAG] Using Remote Reranker: {reranker_model} at {clean_rerank_base}")
                except Exception as e:
                    print(f"[RAG] Failed to init Remote Reranker: {e}")

            # 如果没有配置远程或初始化失败，尝试本地 Flashrank
            if RAGService._reranker_cache is None:
                try:
                    from langchain_community.document_compressors.flashrank_rerank import FlashrankRerank
                    
                    # 针对某些 Pydantic v2 环境的修复：确保模型类已构建
                    try:
                        FlashrankRerank.model_rebuild()
                    except:
                        pass

                    # 动态使用配置中的模型名称，如果没有则回退到轻量级模型
                    target_model = reranker_model if reranker_model else "ms-marco-MiniLM-L-12-v2"
                    
                    RAGService._reranker_cache = FlashrankRerank(
                        model=target_model,
                        cache_dir=self.cache_directory
                    )
                    print(f"[RAG] Using Local Reranker: {target_model}")
                except Exception as e:
                    print(f"[RAG] Failed to init Local Reranker: {e}")
                    RAGService._reranker_cache = None
        self.reranker = RAGService._reranker_cache
        
        # 4. 初始化向量库 (带缓存)
        safe_model_name = self.emb_config['name'].replace("/", "_").replace("-", "_")
        full_collection_name = f"{collection_name}_{safe_model_name}"
        
        vector_store_type = os.environ.get("VECTOR_STORE_TYPE", "chroma").lower()
        
        if full_collection_name not in self._vectorstore_cache:
            if vector_store_type == "pgvector":
                try:
                    from langchain_postgres import PGVector
                    connection_string = os.environ.get("DATABASE_URL")
                    # 将 postgres:// 转换为 postgresql:// 以符合 SQLAlchemy/LangChain 要求
                    if connection_string and connection_string.startswith("postgres://"):
                        connection_string = connection_string.replace("postgres://", "postgresql://", 1)
                    
                    self._vectorstore_cache[full_collection_name] = PGVector(
                        embeddings=self.embeddings,
                        collection_name=full_collection_name,
                        connection=connection_string,
                        use_jsonb=True,
                    )
                    print(f"[RAG] Using PGVector with collection: {full_collection_name}")
                except Exception as e:
                    print(f"[RAG] Failed to init PGVector: {e}. Falling back to Chroma.")
                    self._vectorstore_cache[full_collection_name] = Chroma(
                        collection_name=full_collection_name,
                        embedding_function=self.embeddings,
                        persist_directory=self.persist_directory
                    )
            else:
                self._vectorstore_cache[full_collection_name] = Chroma(
                    collection_name=full_collection_name,
                    embedding_function=self.embeddings,
                    persist_directory=self.persist_directory
                )
        self.vectorstore = self._vectorstore_cache[full_collection_name]
        
        # 5. 初始化 LLM
        self.llm = self._init_llm()

    def _get_model_config(self, model_id: int, model_type: str):
        """获取模型配置，优先级：传入 ID > 全局默认 > 环境变量"""
        model = None
        if model_id:
            model = AIModel.objects.filter(id=model_id, model_type=model_type).first()
        
        if not model:
            # 尝试获取全局默认
            global_config = AIConfig.objects.filter(name="default").first()
            if global_config:
                if model_type == "llm":
                    model = global_config.default_llm
                elif model_type == "embedding":
                    model = global_config.default_embedding
                elif model_type == "rerank":
                    model = global_config.default_rerank

        if model:
            return {
                "name": model.name,
                "provider_type": model.provider.provider_type,
                "base_url": model.provider.base_url,
                "api_key": model.provider.get_decrypted_key()
            }
        
        # 回退到环境变量 (用于兼容旧版本)
        if model_type == "llm":
            return {
                "name": os.environ.get("LLM_MODEL_NAME", "deepseek-chat"),
                "provider_type": "other",
                "base_url": os.environ.get("LLM_API_BASE", "https://api.deepseek.com"),
                "api_key": os.environ.get("LLM_API_KEY")
            }
        elif model_type == "embedding":
            emb_name = os.environ.get("EMBEDDING_MODEL_NAME")
            emb_key = os.environ.get("EMBEDDING_API_KEY")
            emb_base = os.environ.get("EMBEDDING_API_BASE")
            
            if emb_name and emb_key:
                return {
                    "name": emb_name,
                    "provider_type": "other",
                    "base_url": emb_base,
                    "api_key": emb_key
                }
            return {
                "name": "BAAI/bge-small-en-v1.5",
                "provider_type": "local",
                "base_url": None,
                "api_key": None
            }
        elif model_type == "rerank":
            rerank_name = os.environ.get("RERANKER_MODEL_NAME", "bge-reranker-v2-m3")
            rerank_base = os.environ.get("RERANKER_API_BASE")
            rerank_key = os.environ.get("RERANKER_API_KEY")
            if rerank_base:
                return {
                    "name": rerank_name,
                    "provider_type": "other",
                    "base_url": rerank_base,
                    "api_key": rerank_key
                }
            return {
                "name": "ms-marco-MultiBERT-L-12",
                "provider_type": "local",
                "base_url": None,
                "api_key": None
            }
        return {}

    def _init_embeddings(self):
        ptype = self.emb_config['provider_type']
        base_url = self.emb_config['base_url']
        
        # 1. 本地 FastEmbed 路径 (依然保持延迟加载以避开 torch)
        if ptype == "local" or "BAAI" in self.emb_config['name']:
            from langchain_community.embeddings.fastembed import FastEmbedEmbeddings
            return FastEmbedEmbeddings(
                model_name=self.emb_config['name'],
                cache_dir=self.cache_directory
            )
        
        # 2. 自动识别是否为 OpenAI 官方接口
        is_official_openai = base_url and "api.openai.com" in base_url
        
        # 统一使用最新版本的 langchain_openai 适配器
        from langchain_openai import OpenAIEmbeddings
        return OpenAIEmbeddings(
            model=self.emb_config['name'],
            api_key=self.emb_config['api_key'] or "none",
            base_url=base_url,
            # 对于非官方接口（Xinference 等），关闭本地长度检查以防止强行转换 Token
            check_embedding_ctx_length=is_official_openai
        )

    def _init_llm(self):
        ptype = self.llm_config['provider_type']
        if ptype == "ollama":
            from langchain_community.chat_models import ChatOllama
            return ChatOllama(
                model=self.llm_config['name'],
                base_url=self.llm_config['base_url']
            )
        elif ptype == "anthropic":
            from langchain_anthropic import ChatAnthropic
            return ChatAnthropic(
                model_name=self.llm_config['name'],
                api_key=self.llm_config['api_key'],
                base_url=self.llm_config['base_url']
            )
        else:
            # 默认使用 OpenAI 兼容适配器 (OpenAI, DeepSeek, Zhipu 等)
            return ChatOpenAI(
                model=self.llm_config['name'], 
                api_key=self.llm_config['api_key'], 
                base_url=self.llm_config['base_url'],
                streaming=True
            )

    def ingest_document(self, file_path: str, kb_id: int = None, document_id: int = None):
        """Load document, save to DB, and add to vector store with chunk persistence."""
        from .models import KnowledgeDocument, KnowledgeBase, KnowledgeChunk
        kb = KnowledgeBase.objects.filter(id=kb_id).first() if kb_id else KnowledgeBase.objects.first()
        if not kb:
            kb = KnowledgeBase.objects.get_or_create(
                name="默认知识库",
                collection_name="ansflow_docs",
                defaults={"description": "系统默认创建的知识库"}
            )[0]

        ext = os.path.splitext(file_path)[1].lower()
        docs = []
        # ... (解析逻辑保持不变)
        if ext == '.pdf':
            # ...
            try:
                from langchain_community.document_loaders import PyMuPDFLoader
                loader = PyMuPDFLoader(file_path)
                docs = loader.load()
                
                # 检查是否为扫描件或内容极少的 PDF (平均每页少于 100 字符)
                total_chars = sum(len(d.page_content) for d in docs)
                avg_chars_per_page = total_chars / len(docs) if docs else 0
                
                if avg_chars_per_page < 100:
                    print(f"[RAG] PDF appears to be an image or scan (avg {avg_chars_per_page:.1f} chars/page). Switching to OCR...")
                    from unstructured.partition.pdf import partition_pdf
                    from langchain_core.documents import Document
                    
                    elements = partition_pdf(
                        filename=file_path,
                        strategy="hi_res",
                        languages=["chi_sim", "eng"]
                    )
                    docs = [Document(page_content="\n".join([str(el) for el in elements]), 
                                    metadata={"source": os.path.basename(file_path), "method": "ocr"})]
            except Exception as e:
                print(f"[RAG] Advanced PDF loading failed: {e}. Falling back to basic loader.")
                from langchain_community.document_loaders import PyPDFLoader
                loader = PyPDFLoader(file_path)
                docs = loader.load()
        elif ext == '.md':
            from langchain_community.document_loaders import UnstructuredMarkdownLoader
            loader = UnstructuredMarkdownLoader(file_path)
            docs = loader.load()
        else:
            from langchain_community.document_loaders import TextLoader
            loader = TextLoader(file_path)
            docs = loader.load()

        if not docs:
            print(f"[RAG] No content found in {file_path}")
            return 0
        
        full_content = "\n\n".join([doc.page_content for doc in docs])
        
        if document_id:
            kd = KnowledgeDocument.objects.filter(id=document_id).first()
            if kd:
                # 清理旧的分块数据
                KnowledgeChunk.objects.filter(document=kd).delete()
                kd.content = full_content
                kd.status = "processing"
                kd.save()
        
        if not document_id or not kd:
            kd, created = KnowledgeDocument.objects.get_or_create(
                kb=kb,
                title=os.path.basename(file_path),
                defaults={"content": full_content, "file_path": file_path, "source_type": "file", "status": "processing"}
            )
            if not created:
                KnowledgeChunk.objects.filter(document=kd).delete()
                kd.content = full_content
                kd.status = "processing"
                kd.save()
            
        text_splitter = RecursiveCharacterTextSplitter(chunk_size=600, chunk_overlap=120)
        splits = text_splitter.split_documents(docs)
        
        # 3. 存入向量库并持久化分块
        try:
            ids = [f"{kd.id}_{i}" for i in range(len(splits))]
            for i, split in enumerate(splits):
                split.metadata['document_id'] = kd.id
                split.metadata['kb_id'] = kb.id
                split.metadata['chunk_index'] = i
            
            self.vectorstore.add_documents(documents=splits, ids=ids)
            
            # 同步到 SQL
            chunk_objs = [
                KnowledgeChunk(
                    document=kd,
                    content=split.page_content,
                    vector_id=ids[i],
                    index=i,
                    metadata=split.metadata
                ) for i, split in enumerate(splits)
            ]
            KnowledgeChunk.objects.bulk_create(chunk_objs)
            
            kd.status = "ready"
            kd.chunk_count = len(splits)
            kd.save()
            return len(splits)
        except Exception as e:
            kd.status = "error"
            kd.save()
            print(f"[RAG] Ingestion failed: {e}")
            return 0

    def get_retriever(self, kb_id: int = None):
        """Create a Hybrid Search retriever (BM25 + Vector) with Rerank support."""
        from .models import KnowledgeChunk
        
        top_k = self.config.rag_top_k if self.config else 3
        vector_weight = self.config.rag_vector_weight if self.config else 0.7
        bm25_weight = self.config.rag_bm25_weight if self.config else 0.3

        # 1. 向量检索器 (召回数量适当放大，为 Rerank 提供空间)
        vector_retriever = self.vectorstore.as_retriever(search_kwargs={"k": top_k * 4})
        
        # 2. BM25 检索器
        # 从数据库加载所有启用的分块作为 BM25 语料
        chunks_qs = KnowledgeChunk.objects.filter(is_active=True)
        if kb_id:
            chunks_qs = chunks_qs.filter(document__kb_id=kb_id)
        
        chunks = list(chunks_qs.values('content', 'metadata'))
        chunk_list = [c['content'] for c in chunks]
        metadata_list = [c['metadata'] for c in chunks]
        
        if not chunk_list:
            # 只有向量检索时也进行重排序
            self.reranker.top_n = top_k
            return ContextualCompressionRetriever(
                base_compressor=self.reranker, 
                base_retriever=vector_retriever
            )
            
        bm25_retriever = BM25Retriever.from_texts(
            texts=chunk_list, 
            metadatas=metadata_list,
            preprocess_func=chinese_tokenizer
        )
        bm25_retriever.k = top_k * 4
        
        # 3. 混合检索
        ensemble_retriever = EnsembleRetriever(
            retrievers=[vector_retriever, bm25_retriever],
            weights=[vector_weight, bm25_weight]
        )

        # 4. 引入 Rerank 压缩器 (最终只保留 top_k 个)
        if self.reranker:
            try:
                self.reranker.top_n = top_k
                rerank_retriever = ContextualCompressionRetriever(
                    base_compressor=self.reranker, 
                    base_retriever=ensemble_retriever
                )
                return rerank_retriever
            except Exception as e:
                print(f"[RAG] Error setting up Rerank compressor: {e}")
        
        # 如果没有 Reranker，直接返回混合检索结果 (取 top_k 个)
        ensemble_retriever.k = top_k
        return ensemble_retriever

    def rewrite_query(self, query: str):
        """利用 LLM 改写查询，使其更适合在知识库中检索"""
        template = """你是一个专业的搜索优化专家。请将用户的问题改写为一个更适合在技术文档知识库中进行语义检索的查询语句。
如果你认为原始问题已经足够清晰，请直接返回原始问题。
要求：只返回改写后的文本，不要有任何解释。

原始问题：{query}

优化后的查询："""
        prompt = ChatPromptTemplate.from_template(template)
        chain = prompt | self.llm | StrOutputParser()
        try:
            new_query = chain.invoke({"query": query})
            print(f"[RAG] Query Rewrite: '{query}' -> '{new_query.strip()}'")
            return new_query.strip()
        except:
            return query

    def summarize_pipeline_run(self, run_id: int):
        """分析流水线运行记录并生成知识摘要"""
        from apps.pipeline_management.models import PipelineRun, PipelineNodeRun
        try:
            run = PipelineRun.objects.get(id=run_id)
            nodes = PipelineNodeRun.objects.filter(run=run).order_by('start_time')
            
            # 汇总核心日志和步骤
            summary_context = []
            for node in nodes:
                summary_context.append(f"节点: {node.node_label} ({node.node_type})\n状态: {node.status}\n产出: {node.output_data}")

            context_str = "\n---\n".join(summary_context)
            
            template = """你是一个专业的 SRE 专家。请根据以下流水线执行记录，总结出一份技术知识文档。
要求：
1. 提取执行的核心目标和最终产出（如镜像版本、部署环境）。
2. 总结成功的关键步骤。
3. 提炼出可供以后参考的“最佳实践”或“注意事项”。
4. 使用 Markdown 格式。
5. 语言要求：中文。

流水线名称：{name}
执行记录详情：
{context}

知识总结："""
            prompt = ChatPromptTemplate.from_template(template)
            chain = prompt | self.llm | StrOutputParser()
            
            summary = chain.invoke({"name": run.pipeline.name, "context": context_str})
            return summary
        except Exception as e:
            logger.error(f"Failed to summarize pipeline run #{run_id}: {str(e)}")
            return None

    def delete_document(self, document_id: int):
        """Delete all chunks of a document from both SQL and Vector store."""
        from .models import KnowledgeChunk
        chunks = KnowledgeChunk.objects.filter(document_id=document_id)
        vector_ids = list(chunks.values_list('vector_id', flat=True))
        
        if vector_ids:
            try:
                self.vectorstore.delete(ids=vector_ids)
            except Exception as e:
                print(f"[RAG] Failed to delete vectors for document {document_id}: {e}")
                # 备选方案：尝试按 metadata 删除 (部分 Chroma 版本支持)
                try:
                    self.vectorstore._collection.delete(where={"document_id": document_id})
                except:
                    pass
        
        chunks.delete()
        return True

    def delete_chunk(self, chunk_id: int):
        """Delete a single chunk from both SQL and Vector store."""
        from .models import KnowledgeChunk
        chunk = KnowledgeChunk.objects.filter(id=chunk_id).first()
        if not chunk: return False
        
        try:
            self.vectorstore.delete(ids=[chunk.vector_id])
            chunk.delete()
            return True
        except Exception:
            return False

    def update_chunk(self, chunk_id: int, new_content: str):
        """Update a single chunk's content in both SQL and Vector store."""
        from .models import KnowledgeChunk
        from langchain_core.documents import Document
        chunk = KnowledgeChunk.objects.filter(id=chunk_id).first()
        if not chunk: return False
        
        try:
            # 更新向量库 (先删后加)
            self.vectorstore.delete(ids=[chunk.vector_id])
            new_doc = Document(page_content=new_content, metadata=chunk.metadata)
            self.vectorstore.add_documents(documents=[new_doc], ids=[chunk.vector_id])
            
            # 更新 SQL
            chunk.content = new_content
            chunk.save()
            return True
        except Exception:
            return False

    def get_document_chunks(self, document_id: int):
        """Pre-calculate chunks for a document without adding to vector store (for preview)."""
        from .models import KnowledgeDocument
        doc_obj = KnowledgeDocument.objects.filter(id=document_id).first()
        if not doc_obj:
            return []
        
        text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=600, 
            chunk_overlap=120,
            separators=["\n\n", "\n", "。", "！", "？", " ", ""]
        )
        chunks = text_splitter.split_text(doc_obj.content)
        return [{"index": i, "content": chunk, "length": len(chunk)} for i, chunk in enumerate(chunks)]

    def add_knowledge(self, content: str, metadata: dict = None, kb_id: int = None, title: str = None):
        """Manually add a piece of knowledge to both SQL and vector store."""
        from .models import KnowledgeDocument, KnowledgeBase
        try:
            kb = KnowledgeBase.objects.filter(id=kb_id).first() if kb_id else KnowledgeBase.objects.first()
            if not kb:
                kb = KnowledgeBase.objects.create(
                    name="默认知识库",
                    collection_name="ansflow_docs",
                    description="系统默认创建的知识库"
                )

            # 1. 保存到数据库 (持久化用于重索引)
            KnowledgeDocument.objects.create(
                kb=kb,
                title=title or f"Manual Entry {os.urandom(4).hex()}",
                content=content,
                source_type="manual" if not metadata or metadata.get('type') != 'human_verified_knowledge' else "ai_export",
                metadata=metadata or {}
            )

            # 2. 写入向量库
            from langchain_core.documents import Document
            doc = Document(page_content=content, metadata=metadata or {})
            self.vectorstore.add_documents(documents=[doc])
            return True
        except Exception as e:
            print(f"[RAG] Failed to add knowledge: {e}")
            if "RustBindingsAPI" in str(e):
                self._vectorstore_cache.clear()
            return False

    def reindex_all(self, kb_id: int = None):
        """Clear vector store and re-index all documents, syncing chunks to SQL."""
        from .models import KnowledgeDocument, KnowledgeBase, KnowledgeChunk
        kb = KnowledgeBase.objects.filter(id=kb_id).first() if kb_id else KnowledgeBase.objects.first()
        if not kb:
            return 0

        # 1. 彻底清理该知识库的向量集合
        # 既然每个 KB 对应一个 Collection，直接清除集合内所有数据
        try:
            all_ids = self.vectorstore.get()['ids']
            if all_ids:
                batch_size = 5000
                for i in range(0, len(all_ids), batch_size):
                    self.vectorstore.delete(ids=all_ids[i:i+batch_size])
            print(f"[RAG] Collection for KB {kb_id} cleared ({len(all_ids)} vectors).")
        except Exception as e:
            print(f"[RAG] Failed to clear collection during reindex: {e}")
                
        # 2. 清理 SQL 中的分块记录
        KnowledgeChunk.objects.filter(document__kb=kb).delete()
        
        # 3. 重新对库中存在的文档进行索引
        documents = KnowledgeDocument.objects.filter(kb=kb)
        count = 0
        text_splitter = RecursiveCharacterTextSplitter(chunk_size=600, chunk_overlap=120)
        
        for kd in documents:
            from langchain_core.documents import Document
            doc = Document(page_content=kd.content, metadata=kd.metadata)
            splits = text_splitter.split_documents([doc])
            
            ids = [f"{kd.id}_{i}" for i in range(len(splits))]
            for i, split in enumerate(splits):
                split.metadata['document_id'] = kd.id
                split.metadata['kb_id'] = kb.id
                split.metadata['chunk_index'] = i

            self.vectorstore.add_documents(documents=splits, ids=ids)
            
            # 同步到 SQL
            chunk_objs = [
                KnowledgeChunk(
                    document=kd,
                    content=split.page_content,
                    vector_id=ids[i],
                    index=i,
                    metadata=split.metadata
                ) for i, split in enumerate(splits)
            ]
            KnowledgeChunk.objects.bulk_create(chunk_objs)
            
            kd.chunk_count = len(splits)
            kd.save()
            count += 1
            
        return count

    def format_docs(self, docs):
        return "\n\n".join(doc.page_content for doc in docs)

    def retrieve_with_threshold(self, query: str, kb_id: int = None):
        """检索、Rerank 并根据阈值过滤"""
        # get_retriever 已经集成了 Hybrid Search 和 Rerank
        retriever = self.get_retriever(kb_id=kb_id)
        docs = retriever.invoke(query)
        
        threshold = self.config.rag_score_threshold if self.config else 0.0
        if threshold <= 0:
            return docs
            
        try:
            # 过滤逻辑：Rerank 后的文档如果相关性得分（存放在 metadata 中）过低，可以进行过滤
            # 注意：FlashrankRerank 会将得分写入 metadata['relevance_score']
            filtered_docs = []
            for d in docs:
                rerank_score = d.metadata.get('relevance_score') # Flashrank 使用 relevance_score
                if rerank_score is not None:
                    if rerank_score >= threshold:
                        filtered_docs.append(d)
                else:
                    # 对于非 Rerank 路径召回的文档，默认保留
                    filtered_docs.append(d)
                    
            return filtered_docs
        except Exception as e:
            print(f"[RAG] Threshold filtering error: {e}")
            return docs

    def get_chat_chain(self, history_id: int = None, auth_context: dict = None):
        """Returns a LangChain runnable chain for chatting with Hybrid Search support."""
        # 加载历史消息作为上下文 (取最近 20 条)
        chat_memory_str = ""
        if history_id:
            from .models import AIChatMessage
            prev_messages = AIChatMessage.objects.filter(history_id=history_id).order_by('create_time')[:20]
            for m in prev_messages:
                role = "用户" if m.role == 'user' else "助手"
                chat_memory_str += f"{role}: {m.content}\n"

        # 构造授权资源上下文
        auth_str = ""
        if auth_context:
            for r_type, info in auth_context.items():
                auth_str += f"- {info['label']}: " + ", ".join([f"{item['name']}(ID:{item['id']})" for item in info['items']]) + "\n"
        else:
            auth_str = "（暂无可用资源，请建议用户先在平台注册资源）\n"

        template = """{prefix}

【你作为 AnsFlow 助手的特殊能力】
1. 故障诊断：分析日志并给出建议。
2. 资产编排：你可以手写 Ansible Playbook 脚本并将其注册为平台资产。
   - 如果用户要求“写脚本”、“生成剧本”或需要新的修复动作，你必须在回答末尾输出 `__ANSIBLE_DRAFT__: {{"name": "...", "content": "..."}}`。
   - 严禁仅在对话正文中展示 YAML 代码，必须同时通过上述 JSON 标记输出，以便用户一键注册。
3. 流水线编排：你可以根据用户需求生成 DAG。
   - 节点类型：ansible, k8s_deploy, git_clone, docker_build。
   - 如果用户要求编排，或你认为需要通过流水线解决问题，请在回答末尾输出 `__PIPELINE_DRAFT__: {{"nodes": [...], "edges": [...]}}`。
   - **重要联动**：如果你刚输出了 `__ANSIBLE_DRAFT__`，你必须在流水线节点的 `ansible_task_id` 中填入 `"{{{{__ANSIBLE_DRAFT_ID__}}}}"` 作为占位符，以便前端自动关联。

【你当前可用的资源 (RBAC 已授权)】
{auth_resources}

请使用以下检索到的参考内容来回答用户的问题。如果问题涉及 AnsFlow 的编排或诊断，请优先使用你作为助手的特殊能力和上面列出的可用资源。如果现有资源无法满足用户需求，请务必利用“资产编排”能力生成新的脚本。

参考内容：
{context}

对话历史：
{chat_history}

用户问题：{question}

你的回答："""
        
        prompt = ChatPromptTemplate.from_template(template)
        
        def context_retriever(input_data):
            # input_data 这里接收的是原始 query 字符串
            query = input_data
            # 1. 查询改写 (Query Rewrite)
            optimized_query = self.rewrite_query(query)
            # 2. 检索并根据阈值过滤 (内部已包含 Rerank)
            docs = self.retrieve_with_threshold(optimized_query)
            return self.format_docs(docs)

        # 将静态内容作为变量注入，避免大括号解析错误
        chain = (
            {
                "context": context_retriever, 
                "question": RunnablePassthrough(),
                "prefix": lambda x: self.personality['prefix'],
                "auth_resources": lambda x: auth_str,
                "chat_history": lambda x: chat_memory_str
            }
            | prompt
            | self.llm
            | StrOutputParser()
        )
        return chain

    def chat_stream(self, question: str, history_id: int = None, auth_context: dict = None):
        chain = self.get_chat_chain(history_id=history_id, auth_context=auth_context)
        for chunk in chain.stream(question):
            yield chunk

    def diagnose_log(self, log_content: str, context_info: dict, auth_context: dict = None):
        auth_str = ""
        if auth_context:
            auth_str = "\n【当前用户可用的修复资源 (仅限以下)】\n"
            for r_type, info in auth_context.items():
                auth_str += f"- {info['label']}: " + ", ".join([f"{item['name']}(ID:{item['id']})" for item in info['items']]) + "\n"

        template = """{prefix}
作为专业的 SRE 运维专家，请分析以下执行日志并给出诊断结论和修复建议。
{auth_str}
【执行上下文】
- 类型: {target_type}
- 名称: {target_name}
- 错误摘要: {error_summary}

【错误日志截取】
{log_content}

【参考知识库】
{context}

请按以下格式回答（重要提示：必须严格针对上面【执行上下文】中指定的告警/任务名称进行诊断，【参考知识库】仅提供解决思路，切勿照抄知识库中的其他无关告警名称或旧的诊断回复）：
### 🔍 故障根因
(描述为什么报错)

### 🛠️ 修复建议
(给出具体的步骤。如果【可用修复资源】中有匹配的流水线或任务，请明确指出并建议用户执行，必须指明 ID。**如果现有资源不完全匹配，你必须利用资产编排能力手写修复脚本**)

(如果你决定手写新的 Ansible Playbook，你必须在回答的末尾以 `__ANSIBLE_DRAFT__: {{"name": "...", "content": "..."}}` 的格式输出纯 JSON。严禁只在正文中展示代码而不输出此标记。请确保 JSON 结构符合规范且内容为 YAML 格式的完整 Playbook)

(如果你认为需要编排全新的流水线步骤，请根据以下【节点类型规范】生成一个流水线草案，并在回答的最后以 `__PIPELINE_DRAFT__: {{"nodes": [...], "edges": [...]}}` 的格式输出纯 JSON。**如果你刚输出了 __ANSIBLE_DRAFT__，你必须在 ansible_task_id 中填入 "{{{{__ANSIBLE_DRAFT_ID__}}}}" 作为占位符**)
**注意：每个 node 必须包含 {{"id": "...", "type": "...", "position": {{"x": 0, "y": 0}}, "data": {{"label": "...", "ansible_task_id": ...}}}} 这种完整结构。** 请根据节点顺序自动递增 x 坐标（间距 300）。

【节点类型规范】
- ansible: 执行 Ansible 任务。参数放在 data 中: {{'ansible_task_id': int 或 string, 'label': string}}
- k8s_deploy: 部署 K8s 资源。参数放在 data 中: {{'cluster_id': int, 'manifest': string, 'label': string}}
- git_clone: 克隆代码。参数放在 data 中: {{'repo_url': string, 'branch': string}}
- docker_build: 构建镜像。参数放在 data 中: {{'image_name': string, 'dockerfile': string}}

### 💡 预防措施
(如何避免下次发生)
"""
        prompt = ChatPromptTemplate.from_template(template)

        def context_retriever(input_data):
            # 构建更精准的检索词
            search_text = f"诊断 {input_data['target_name']} 错误: {input_data['error_summary']}"
            if len(input_data["log_content"]) > 0:
                # 提取日志或告警详情的精简特征
                search_text += " " + input_data["log_content"][:200].replace('\n', ' ')
            
            optimized_query = self.rewrite_query(search_text)
            docs = self.retrieve_with_threshold(optimized_query)
            
            # 记录引用的文档元数据，供后续流式输出
            input_data['_referenced_docs'] = [
                {"id": d.metadata.get('id'), "title": d.metadata.get('title') or d.page_content[:30]} 
                for d in docs if d.metadata.get('id')
            ]
            
            return self.format_docs(docs)

        # 这里我们需要先运行一次检索，以便获取 _referenced_docs
        # 但由于 LangChain Chain 的特性，检索通常在运行中触发。
        # 我们改为先手动检索。
        
        search_text = f"诊断 {context_info.get('name')} 错误: {context_info.get('summary')}"
        if log_content:
            search_text += " " + log_content[:200].replace('\n', ' ')
        
        optimized_query = self.rewrite_query(search_text)
        referenced_docs = self.retrieve_with_threshold(optimized_query)
        
        # 输出引用标记
        import json
        refs = [
            {"id": d.metadata.get('document_id'), "title": d.metadata.get('title') or "相关文档"} 
            for d in referenced_docs if d.metadata.get('document_id')
        ]
        if refs:
            yield f"__REFERENCES__:{json.dumps(refs)}\n"

        chain = (
            {
                "context": lambda x: self.format_docs(referenced_docs), 
                "target_type": lambda x: x["target_type"],
                "target_name": lambda x: x["target_name"],
                "error_summary": lambda x: x["error_summary"],
                "log_content": lambda x: x["log_content"],
                "prefix": lambda x: self.personality['prefix'],
                "auth_str": lambda x: auth_str
            }
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
            for r_type, info in context_data.items():
                context_str += f"\n【可用 {info['label']} 列表】\n"
                for item in info['items']:
                    context_str += f"- ID: {item['id']}, 名称: {item['name']}\n"

        template = """你是一个专业的 AnsFlow 流水线设计专家。
请根据用户的需求，生成一个符合 ReactFlow 规范的 JSON 格式 DAG 流水线数据。
{dynamic_context}
【节点类型规范】
- ansible: 执行 Ansible 任务。参数放在 data 中: {{'ansible_task_id': int, 'label': string}}
- k8s_deploy: 部署 K8s 资源。参数放在 data 中: {{'cluster_id': int, 'manifest': string, 'label': string}}
- git_clone: 克隆代码。参数放在 data 中: {{'repo_url': string, 'branch': string}}
- docker_build: 构建镜像。参数放在 data 中: {{'image_name': string, 'dockerfile': string}}

【输出要求】
1. 只输出纯 JSON 格式，不要包含 Markdown 标记或任何解释。
2. JSON 结构必须包含 'nodes' 和 'edges'。
3. 如果用户提到的任务名称或集群名称在【可用列表】中存在，请务必使用对应的正确 ID。
4. 节点位置(position)请合理计算，使其水平从左向右排列，间距 300px，y 轴设为 100。
5. 边(edges)的 id 格式为 'e-source-target'。
6. 严禁使用列表中不存在的 ID。
7. 每个 node 必须包含 {{"id": "...", "type": "...", "position": {{"x": 0, "y": 0}}, "data": {{"label": "...", "ansible_task_id": ...}}}} 这种完整结构。

用户需求：{prompt_text}

JSON 输出："""
        prompt = ChatPromptTemplate.from_template(template)
        chain = (
            {"prompt_text": RunnablePassthrough(), "dynamic_context": lambda x: context_str}
            | prompt 
            | self.llm 
            | StrOutputParser()
        )
        return chain.invoke(prompt_text)

    def refine_dag(self, prompt_text: str, current_nodes: list, current_edges: list, auth_context: dict = None):
        """Refines an existing DAG structure based on user instructions."""
        context_str = ""
        if auth_context:
            for r_type, info in auth_context.items():
                context_str += f"\n【可用 {info['label']} 列表】\n"
                for item in info['items']:
                    context_str += f"- ID: {item['id']}, 名称: {item['name']}\n"

        import json
        current_state_json = json.dumps({"nodes": current_nodes, "edges": current_edges}, ensure_ascii=False)

        template = """你是一个专业的 AnsFlow 流水线重构专家。
你现在的任务是根据用户的指令，修改现有的流水线结构。

【当前流水线状态】
{current_state}

【可用资源列表】
{dynamic_context}

【修改要求】
1. 必须基于【当前流水线状态】进行修改，保持未被要求修改的部分不变。
2. 只输出纯 JSON 格式，包含完整的 'nodes' 和 'edges'。
3. 确保节点 ID 唯一，且 position 坐标合理（水平间距 300px）。
4. 每个 node 必须包含 {{"id": "...", "type": "...", "position": {{"x": 0, "y": 0}}, "data": {{"label": "...", "ansible_task_id": ...}}}} 这种完整结构。
5. 如果用户要求删除节点，请确保同时删除相关的边。
6. 严禁使用列表中不存在的资源 ID。
7. 不要输出 Markdown 标记，只输出 JSON。

用户修改指令：{prompt_text}

JSON 输出："""
        
        prompt = ChatPromptTemplate.from_template(template)
        chain = (
            {
                "prompt_text": lambda x: x["prompt"],
                "current_state": lambda x: x["state"],
                "dynamic_context": lambda x: x["context"]
            }
            | prompt 
            | self.llm 
            | StrOutputParser()
        )
        return chain.invoke({
            "prompt": prompt_text,
            "state": current_state_json,
            "context": context_str
        })

    def suggest_node_params(self, node_type: str, current_data: dict, pipeline_context: dict):
        """Suggests parameters for a specific node based on its type and pipeline context."""
        template = """你是一个专业的 AnsFlow 流水线配置专家。
请根据当前的节点类型和全量流水线上下文，为用户推荐最佳的配置参数。

【目标节点类型】
{node_type}

【当前已填参数】
{current_data}

【全量流水线上下文 (已存在的节点)】
{pipeline_context}

【任务】
请分析流水线逻辑，为该节点生成最合适的配置。
例如：如果是 docker_build 节点，请根据 git_clone 节点的仓库推测基础镜像和构建指令。
如果是 k8s_deploy 节点，请生成符合规范的 YAML。

【输出要求】
1. 只输出纯 JSON 格式，包含建议的字段及其值。
2. 不要包含 Markdown 标记。
3. 字段名必须与 AnsFlow 的规范一致。

JSON 输出："""
        import json
        prompt = ChatPromptTemplate.from_template(template)
        chain = (
            {
                "node_type": lambda x: x["node_type"],
                "current_data": lambda x: json.dumps(x["current_data"], ensure_ascii=False),
                "pipeline_context": lambda x: json.dumps(x["pipeline_context"], ensure_ascii=False)
            }
            | prompt 
            | self.llm 
            | StrOutputParser()
        )
        return chain.invoke({
            "node_type": node_type,
            "current_data": current_data,
            "pipeline_context": pipeline_context
        })

    def explain_pipeline(self, nodes: list, edges: list):
        """Generates a step-by-step explanation and risk assessment for a pipeline."""
        import json
        pipeline_json = json.dumps({"nodes": nodes, "edges": edges}, ensure_ascii=False)

        template = """你是一个专业的 SRE 审计专家。
请分析以下 AnsFlow 流水线配置，并给出通俗易懂的执行预案说明。

【流水线配置】
{pipeline_json}

【输出要求】
1. **执行步骤**：按执行顺序描述每一步会做什么（例如：第一步，克隆代码；第二步，构建镜像...）。
2. **潜在影响**：告知用户该操作是否会导致业务中断、资源消耗或配置覆盖。
3. **安全建议**：如果发现明显的配置风险（如缺少凭据、超时设置过短等），请指出。
4. 使用 Markdown 格式输出，保持客观、严谨。

请开始你的分析："""
        
        prompt = ChatPromptTemplate.from_template(template)
        chain = (
            {"pipeline_json": RunnablePassthrough()}
            | prompt 
            | self.llm 
            | StrOutputParser()
        )
        return chain.invoke(pipeline_json)
