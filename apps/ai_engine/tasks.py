import logging
from celery import shared_task
from .rag_service import RAGService
from .models import KnowledgeDocument

logger = logging.getLogger(__name__)

from .models import KnowledgeDocument, AIConfig

@shared_task(name="apps.ai_engine.tasks.auto_summarize_run_task")
def auto_summarize_run_task(run_id: int):
    """流水线成功后自动总结经验并存入知识库"""
    rag = RAGService()
    summary = rag.summarize_pipeline_run(run_id)
    
    if summary:
        # 获取默认知识库
        config = AIConfig.objects.filter(name="default").first()
        kb = config.default_kb if config else None
        
        from apps.pipeline_management.models import PipelineRun
        run = PipelineRun.objects.get(id=run_id)
        
        # 存入知识库
        rag.add_knowledge(
            content=summary,
            title=f"部署经验: {run.pipeline.name} (Run #{run_id})",
            metadata={
                "source": f"pipeline_run_{run_id}",
                "type": "automated_experience",
                "pipeline": run.pipeline.name
            },
            kb_id=kb.id if kb else None
        )
        return f"Successfully summarized run #{run_id}"
    return f"Failed or skipped summary for run #{run_id}"

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
        # 调用 RAG 服务进行实际处理 (内部会处理状态更新)
        rag = RAGService()
        count = rag.ingest_document(doc.file_path, kb_id=doc.kb_id, document_id=document_id)
        
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
