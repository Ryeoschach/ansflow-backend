# AnsFlow Backend

English | [中文说明](./README_ZH.md)

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
- **Knowledge Loop**: Supports one-click saving of AI responses or diagnosis results into the RAG system for continuous learning.
- **Physical Sync**: Fully synchronized document lifecycle—deleting a document now physically removes the source file and thoroughly cleans its vector index.
- **Semantic Cache**: Millisecond-level response for recurring issues using high-confidence vector similarity search.
- **Categorized History**: Automatically distinguishes between "Diagnosis" and "General Chat" with support for title-based search.
- **Typing Animation**: Sophisticated three-dot jumping animation for a more interactive and lively AI thinking process.
- **WebSocket Streaming**: Django Channels integrated for real-time typewriter-effect streaming output, enhancing chat interaction responsive speed.

### 2. Task Pulse Monitoring (SRE Center)
- **Real-time Status**: High-frequency monitoring of asynchronous task health and execution trends.
- **Pulse Heartbeat**: Background monitor (`pulse_monitor`) tracks task throughput and failure rates.

### 3. Intelligent Diagnosis (DevOps + AI)
- **Execution Memory**: Node trace view automatically displays historical diagnosis results to prevent redundant analysis.
- **Root Cause Analysis**: AI automatically captures Error Logs and provides professional "three-stage" diagnostic reports.

### 4. SRE Alert Self-healing (SRE Center)
- **Real-time Tracking**: Integrated progress bars and live status updates for self-healing pipelines within the Alert Center.
- **Full Traceability**: Accurate identification of "Auto-Triggered" vs. "Manual" healing sources with visual feedback.
- **Strong Consistency**: Signal-based status synchronization between pipeline execution and alert event records.
- **Knowledge Export**: Seamlessly export alert diagnosis conclusions to the system's operational knowledge base.
- **Webhook Token Authentication**: Secure the Prometheus Alertmanager Webhook endpoint `/api/v1/sre/alerts/receive/` using a configurable token (`webhook_token` in Config Center). Supports `Bearer <token>` HTTP header and `?token=<token>` URL query param. Backward compatible (auto-allows requests if token configuration is left empty).
- **Self-Healing Circuit Breaker**: Prevent infinite execution loops under faulty environments by setting frequency rules on alert fingerprints. Auto-transitions healing pipeline to an approval state (`awaiting_approval`) and files a ticket when threshold is breached.

### 5. AIGC Intent Orchestration (Pipeline Gen)
- **Natural Language Orchestration**: Users input requirements in plain text, and AI generates a DAG JSON structure compliant with ReactFlow for the frontend canvas.

### 6. Config Center & Compliance (Security & System)
- **Custom AI Prompt Templates**: Dynamic prompt configuration for 7 LLM scenarios (RAG Q&A, diagnosis, alert analysis, DAG gen/refine, pipeline explain, OCR). Includes required variable placeholder verification and failsafe code defaults.
- **Multi-channel Notifications**: central configuration of Feishu/DingTalk bot Webhooks, notification level filtering, and a refined event whitelist (`pipeline_start`, `pipeline_result`, `approval_requested`, `approval_result`, `task_result`) with env variable fallback.
- **Security compliance & MLPS 2.0**: Out-of-the-box support for host compliance checks, tracking security drifts, and automated remediation aligned with National Cyber Protection Level 3 standards.

### 7. Project Multi-Tenancy & Asset Sharing
- **Workspace Isolation**: Multi-workspace/project paradigm (`Project` and `ProjectMember`) with tenant workspace resolution via `X-Project-ID` request headers.
- **Row-level Tenancy Protection**: Hard isolation on Hosts, Credentials, Pipelines, K8s Clusters, Ansible Tasks, and SRE Policies using database-level project checks via `SmartRBACPermission` and `DataScopeMixin`.
- **Cross-Project Asset Sharing (ProjectAssetShare)**：Support controlled resource sharing with targeted permissions: `read` (read-only), `use` (executable/referenceable in pipelines without revealing secrets), and `full` (complete control), coupled with auditing and origin-restricted revocation.

### 8. Multi-Dimensional Operation Reports (system_reports)
- **Database Metrics Aggregation**: High-performance SQL analytical queries spanning Django Models for alert history, Celery task logs, Ansible pipeline durations, and host compliance score registers.
- **Asynchronous Report Exporter**: Dispatches Celery-backed worker jobs to query, compile, package, and compress CSV logs for offline analytical download.
- **Project-Level Scope Restriction**: Integrates with workspace filters to restrict reporting data visibility in accordance with tenant scopes.

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

## Initialization

### Database Initialization

```bash
uv run python manage.py migrate
uv run python manage.py createsuperuser
uv run python manage.py sync_perms # Sync permission semantics
```

---

## API Documentation

Swagger/OpenAPI documentation is available at `/api/docs/` after starting the server.

## License

Private - All Rights Reserved
