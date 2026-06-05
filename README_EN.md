# AnsFlow Backend

<p align="left">
  <a href="https://ansflow.cyfee.com"><img src="https://img.shields.io/badge/Python-3.12+-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.12+"></a>
  <a href="https://ansflow.cyfee.com"><img src="https://img.shields.io/badge/Django-5.2-092E20?style=for-the-badge&logo=django&logoColor=white" alt="Django 5.2"></a>
  <a href="https://ansflow.cyfee.com"><img src="https://img.shields.io/badge/Celery-Ready-37814A?style=for-the-badge&logo=celery" alt="Celery"></a>
  <a href="https://ansflow.cyfee.com"><img src="https://img.shields.io/badge/Docker-Compose-2496ED?style=for-the-badge&logo=docker&logoColor=white" alt="Docker Compose"></a>
  <a href="./README.md"><img src="https://img.shields.io/badge/Lang-中文说明-red?style=for-the-badge" alt="中文说明"></a>
</p>

English | [中文说明](./README.md)

AnsFlow Backend is the Django service layer for AnsFlow, providing REST APIs, WebSocket streams, Celery tasks, SmartRBAC permissions, AI/RAG orchestration, SRE self-healing, and Ansible/Kubernetes execution.

- Product site and full documentation: [https://ansflow.cyfee.com](https://ansflow.cyfee.com)
- SRE Diagnosis Center supports template-based diagnosis, multi-source log and metric collection, CI/CD and Ansible context analysis, plus log/metric query preview APIs for service mappings.

### GitHub Repositories
- Portal Web: [Ryeoschach/ansflow-web](https://github.com/Ryeoschach/ansflow-web)
- Frontend: [Ryeoschach/ansflow-frontend](https://github.com/Ryeoschach/ansflow-frontend)
- Backend: [Ryeoschach/ansflow-backend](https://github.com/Ryeoschach/ansflow-backend)

### Gitee Mirrors
- Portal Web: [cyfee/ansflow-web](https://gitee.com/cyfee/ansflow-web)
- Frontend: [cyfee/ansflow-frontend](https://gitee.com/cyfee/ansflow-frontend)
- Backend: [cyfee/ansflow-backend](https://gitee.com/cyfee/ansflow-backend)

## Stack

- Django 5.2, Django REST Framework, Django Channels
- Celery, Redis, PostgreSQL/pgvector
- LangChain, ChromaDB/pgvector, FastEmbed
- Ansible Runner, Kubernetes Python Client

## Local Development

```bash
uv sync
cp .env.example .env
uv run python manage.py migrate
uv run python manage.py runserver
```

Run background workers in separate terminals when developing async features:

```bash
uv run celery -A config worker --loglevel=info -P solo
uv run celery -A config beat --loglevel=info --scheduler django_celery_beat.schedulers:DatabaseScheduler
uv run python manage.py pulse_monitor
```

## Docker Compose

```bash
docker compose up -d
docker compose ps
docker compose logs -f ansflow-api ansflow-worker ansflow-init
```

The compose `ansflow-init` service handles database migrations, static file collection, and optional system initialization based on `.env` settings. Detailed deployment and usage instructions are maintained in the AnsFlow Web documentation portal.

## SRE Observability Preview APIs

```bash
GET  /api/v1/sre/observability-datasources/capabilities/
POST /api/v1/sre/observed-services/{id}/preview-logs/
POST /api/v1/sre/observed-services/{id}/preview-metrics/
GET  /api/v1/sre/diagnosis-templates/
POST /api/v1/sre/diagnosis-templates/{id}/run/
```

These endpoints are used for datasource capability discovery, service log preview, and metric preview before running a timepoint diagnosis.
Diagnosis templates maintain global/project scenario packages. A template can configure CI/CD, Ansible, service log, service metric, alert, and approval context collection; each run stores a template snapshot and normalizes multi-source logs and metrics into `log_contexts`, `metric_contexts`, and a unified `evidence_index`. See the AnsFlow Web documentation portal for the full workflow.

## License

Private - All Rights Reserved
