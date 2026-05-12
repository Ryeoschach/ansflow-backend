from django.apps import AppConfig

class PipelineManagementConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.pipeline_management'
    verbose_name = '流水线编排管理'

    def ready(self):
        # 注册审批拦截模板
        from apps.approval_center.registry import approval_registry
        approval_registry.register(
            code="pipeline:run",
            name="流水线运行 (生产环境发布拦截)",
            name_en="Pipeline Execution (Production Intercept)",
            icon="PartitionOutlined",
            description="当流水线被触发执行前，命中的策略将挂起请求并等待人工审批。",
            description_en="Before the pipeline is triggered, hitting policies will suspend the request and wait for manual approval."
        )
