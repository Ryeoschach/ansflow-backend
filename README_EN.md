# AnsFlow Backend

Enterprise-level DevOps pipeline platform backend, built with Django 5.2 + DRF. Integrated with **RAG Knowledge Base**, **Intelligent Diagnosis**, and **SRE Self-healing** capabilities.

**Current Version**: v2.0.0 (build: 2026-05-08)  
**Core Capabilities**: DevOps + SRE + RAG AI Assistant

---

## Tech Stack

| Category | Technology | Description |
|------|------|------|
| Framework | Django 5.2 + DRF 3.16 | Core Web Framework |
| AI Orchestration | LangChain | RAG pipeline and LLM interaction driver |
| Vector Database | ChromaDB | Local vector storage for semantic search |
| Embedding Engine | FastEmbed | Lightweight ONNX runtime, no PyTorch dependency |
| Large Language Model | DeepSeek-V3 | Core reasoning engine (OpenAI SDK compatible) |
| Async Tasks | Celery 5.x + Redis | Distributed task queue |
| Real-time Comm | Django Channels + WebSocket | Real-time pipeline log streaming |
| Container Orch | Kubernetes Python Client | K8s cluster management |
| IaC | Ansible Runner | Bulk host task execution |

---

## Project Structure

```
backend/
├── apps/                                   # Business Application Modules
│   ├── ai_engine/                           # AI Intelligence Center (RAG + LLM)
│   │   ├── models.py                        # KnowledgeBase / ChatHistory / Message
│   │   ├── rag_service.py                   # RAG Core Logic (Vector/Embedding/LLM)
│   │   ├── views.py                         # Chat, Diagnosis, AIGC Orchestration APIs
│   │   └── tasks.py                         # Async tasks for document vectorization
│   ├── sre_management/                      # SRE Intelligent Ops (Alerts/Healing)
│   │   ├── models.py                        # AlertEvent / SelfHealingPolicy
│   │   ├── views.py                         # Alert Webhook Gateway
│   │   └── tasks.py                         # Async AI diagnosis & self-healing matching
│   ├── rbac_permission/                     # User/Role/Perm/Menu/Audit Log
│   ├── pipeline_management/                 # Pipeline DAG & Scheduling (Core)
│   ├── host_management/                     # Host/Env/Platform/Resource Pool
│   ├── task_management/                     # Ansible Task Center
│   ├── k8s_management/                      # K8s Clusters + Helm Management
│   ├── registry_management/                  # Docker Registry + Artifact Management
│   ├── approval_center/                      # Workflow Approval Engine
│   ├── credentials_management/              # Secure Credential Vault
│   ├── config_center/                        # Configuration Center (Hot-reload)
│   └── system_management/                    # System Settings/Health/Notify
├── config/                                   # Project Configuration
├── utils/                                   # Common Utilities
├── chroma_db/                               # Vector Database Persistence Directory
└── .model_cache/                            # Embedding Model Cache Directory
```

---

## Intelligent Ops Features

### 1. RAG Knowledge Assistant (AI Engine)
- **Semantic Q&A**: Precise Q&A powered by LLM based on local operation manuals (Markdown/Text) stored in vector database.
- **Document Vectorization**: Bulk import technical documents via `manage.py ingest_docs`.
- **Streaming Chat**: Real-time response based on SSE (Server-Sent Events).

### 2. Intelligent Diagnosis (DevOps + AI)
- **Root Cause Analysis**: AI automatically captures the last Error Log when a pipeline node or Ansible task fails.
- **Three-stage Diagnosis**: Automatically outputs "Root Cause", "Remediation Steps", and "Prevention Measures".

### 3. SRE Alert Self-healing (SRE Center)
- **Alert Gateway**: Receives Prometheus/Alertmanager alert webhooks.
- **Async Diagnosis**: Automatically triggers AI diagnosis upon alert arrival, analyzing labels and matching self-healing knowledge.
- **Closed-loop Execution**: Recommends matching self-healing pipelines (DAG), allowing one-click remediation.

### 4. AIGC Intent Orchestration (Pipeline Gen)
- **Natural Language Orchestration**: Users input requirements in plain text, and AI generates a DAG JSON structure compliant with ReactFlow for the frontend canvas.

---

## Quick Start

### Core Environment Config (.env)

New AI modules require the following environment variables:

```bash
# DeepSeek / OpenAI Configuration
LLM_API_KEY=sk-xxxx
LLM_API_BASE=https://api.deepseek.com

# macOS Celery Compatibility Config
OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES
TOKENIZERS_PARALLELISM=false
```

### Start Services

```bash
# Start API Service
uv run python manage.py runserver

# Start Celery Worker (Required for AI async diagnosis)
# Threads or solo pool recommended on macOS for AI library stability
uv run celery -A config worker --loglevel=info -P threads
```

---

## API Documentation

Swagger/OpenAPI documentation is available at `/api/docs/` after starting the server.

## License

Private - All Rights Reserved
