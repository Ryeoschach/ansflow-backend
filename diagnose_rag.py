import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.base')
django.setup()

from apps.ai_engine.rag_service import RAGService

def diagnose_chroma():
    print("正在尝试初始化 RAGService...")
    try:
        rag = RAGService()
        print("✅ RAGService 初始化成功！")
        
        print("正在尝试搜索向量库...")
        results = rag.vectorstore.similarity_search("test", k=1)
        print(f"✅ 搜索测试成功，获取到 {len(results)} 条结果。")
    except Exception as e:
        print(f"❌ 初始化失败: {type(e).__name__}: {str(e)}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    diagnose_chroma()
