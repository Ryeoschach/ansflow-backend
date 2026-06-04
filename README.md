# AnsFlow Backend

<p align="left">
  <a href="https://ansflow.cyfee.com"><img src="https://img.shields.io/badge/Python-3.12+-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.12+"></a>
  <a href="https://ansflow.cyfee.com"><img src="https://img.shields.io/badge/Django-5.2-092E20?style=for-the-badge&logo=django&logoColor=white" alt="Django 5.2"></a>
  <a href="https://ansflow.cyfee.com"><img src="https://img.shields.io/badge/Celery-Ready-37814A?style=for-the-badge&logo=celery" alt="Celery"></a>
  <a href="https://ansflow.cyfee.com"><img src="https://img.shields.io/badge/Docker-Compose-2496ED?style=for-the-badge&logo=docker&logoColor=white" alt="Docker Compose"></a>
  <a href="./README_EN.md"><img src="https://img.shields.io/badge/Lang-English-red?style=for-the-badge" alt="English"></a>
</p>

中文说明 | [English](./README_EN.md)

AnsFlow Backend 是 AnsFlow 的 Django 服务端，负责 REST API、WebSocket 实时推送、Celery 异步任务、SmartRBAC 权限、AI/RAG 编排、SRE 自愈以及 Ansible/Kubernetes 执行能力。

- 产品展示与完整文档：[https://ansflow.cyfee.com](https://ansflow.cyfee.com)
- SRE 诊断中心支持 VictoriaMetrics、VictoriaLogs、Elasticsearch、Loki 与通用 HTTP 日志网关接入，并提供服务映射的日志/指标查询预览接口。

### GitHub 仓库
- 门户网站：[Ryeoschach/ansflow-web](https://github.com/Ryeoschach/ansflow-web)
- 前端仓库：[Ryeoschach/ansflow-frontend](https://github.com/Ryeoschach/ansflow-frontend)
- 后端仓库：[Ryeoschach/ansflow-backend](https://github.com/Ryeoschach/ansflow-backend)

### Gitee 镜像仓库
- 门户网站：[cyfee/ansflow-web](https://gitee.com/cyfee/ansflow-web)
- 前端仓库：[cyfee/ansflow-frontend](https://gitee.com/cyfee/ansflow-frontend)
- 后端仓库：[cyfee/ansflow-backend](https://gitee.com/cyfee/ansflow-backend)

## 技术栈

- Django 5.2、Django REST Framework、Django Channels
- Celery、Redis、PostgreSQL/pgvector
- LangChain、ChromaDB/pgvector、FastEmbed
- Ansible Runner、Kubernetes Python Client

## 本地开发

```bash
uv sync
cp .env.example .env
uv run python manage.py migrate
uv run python manage.py runserver
```

开发异步任务相关功能时，在独立终端启动后台进程：

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

Compose 中的 `ansflow-init` 服务会根据 `.env` 设置自动处理数据库迁移、静态文件收集和可选系统初始化。详细部署与使用说明统一维护在 AnsFlow Web 文档门户中。

## SRE 观测调试接口

```bash
GET  /api/v1/sre/observability-datasources/capabilities/
POST /api/v1/sre/observed-services/{id}/preview-logs/
POST /api/v1/sre/observed-services/{id}/preview-metrics/
GET  /api/v1/sre/diagnosis-templates/
POST /api/v1/sre/diagnosis-templates/{id}/run/
```

这些接口用于数据源能力发现、服务映射日志预览和指标预览，便于在时间点诊断前验证标签选择器、字段映射和响应映射是否正确。
诊断模板接口用于维护全局/项目级场景诊断包；第一版内置 CI/CD 发布诊断模板，详细使用说明见 AnsFlow Web 文档门户。

## License

Private - All Rights Reserved
