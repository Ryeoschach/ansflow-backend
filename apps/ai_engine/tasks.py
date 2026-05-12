import logging
from celery import shared_task
from .rag_service import RAGService
from .models import KnowledgeDocument

logger = logging.getLogger(__name__)

@shared_task(name="apps.ai_engine.tasks.ingest_document_task")
def ingest_document_task(document_id: int):
    """
    异步处理文档切片与向量化
    """
    doc = KnowledgeDocument.objects.filter(id=document_id).first()
    if not doc or not doc.file_path:
        logger.error(f"[RAG Task] Document {document_id} not found or has no file path.")
        return False
    
    try:
        # 更新状态为处理中
        doc.status = 'processing'
        doc.save(update_fields=['status'])
        
        # 调用 RAG 服务进行实际处理
        rag = RAGService()
        # 这里的 ingest_document 内部已经处理了切片存入 SQL 和 Vector 的逻辑
        count = rag.ingest_document(doc.file_path, kb_id=doc.kb_id)
        
        logger.info(f"[RAG Task] Successfully ingested document {document_id}, chunks: {count}")
        return True
    except Exception as e:
        logger.exception(f"[RAG Task] Failed to ingest document {document_id}: {str(e)}")
        doc.status = 'error'
        doc.save(update_fields=['status'])
        return False

@shared_task(name="apps.ai_engine.tasks.reindex_kb_task")
def reindex_kb_task(kb_id: int):
    """
    异步重建知识库索引
    """
    try:
        rag = RAGService()
        count = rag.reindex_all(kb_id=kb_id)
        logger.info(f"[RAG Task] Successfully reindexed knowledge base {kb_id}, documents: {count}")
        return True
    except Exception as e:
        logger.exception(f"[RAG Task] Failed to reindex knowledge base {kb_id}: {str(e)}")
        return False
