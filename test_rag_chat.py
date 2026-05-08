import os
import django

# Set up Django environment
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.development')
django.setup()

from apps.ai_engine.rag_service import RAGService

def test_chat():
    question = "如何扩容 K8s 集群节点？"
    print(f"User: {question}")
    
    rag_service = RAGService()
    print("AI: ", end="", flush=True)
    
    for chunk in rag_service.chat_stream(question):
        print(chunk, end="", flush=True)
    print("\n")

if __name__ == "__main__":
    test_chat()
