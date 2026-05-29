from django.core.management.base import BaseCommand
from apps.ai_engine.rag_service import RAGService
import os

class Command(BaseCommand):
    help = 'Ingest a document into the AI knowledge base.'

    def add_arguments(self, parser):
        parser.add_argument('file_path', type=str, help='Path to the markdown or text file.')

    def handle(self, *args, **kwargs):
        file_path = kwargs['file_path']
        if not os.path.exists(file_path):
            self.stdout.write(self.style.ERROR(f"File not found: {file_path}"))
            return

        self.stdout.write(f"Ingesting {file_path}...")
        rag_service = RAGService()
        chunks = rag_service.ingest_document(file_path)
        self.stdout.write(self.style.SUCCESS(f"Successfully ingested {chunks} chunks into ChromaDB."))
