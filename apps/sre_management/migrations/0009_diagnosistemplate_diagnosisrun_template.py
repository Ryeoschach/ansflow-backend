from django.db import migrations, models
import django.db.models.deletion


def seed_builtin_templates(apps, schema_editor):
    DiagnosisTemplate = apps.get_model('sre_management', 'DiagnosisTemplate')
    templates = [
        {
            'code': 'ci_pipeline_failure',
            'name': '流水线失败诊断',
            'description': '聚焦失败流水线、失败节点日志、审批记录和相关告警，输出失败原因与处置建议。',
            'content': {
                'target_type': 'pipeline_run',
                'context_collection': {
                    'pipeline_run': True,
                    'failed_nodes': True,
                    'node_logs': True,
                    'approval_records': True,
                    'related_alerts': True,
                    'service_logs': False,
                    'metrics': False,
                    'ansible_execution': False,
                },
                'log_keywords': ['error', 'failed', 'exception', 'timeout', 'denied', 'permission', 'exit code', 'traceback'],
                'prompt_template': '{prefix}\n你是 AnsFlow SRE 诊断助手，请基于以下 CI/CD 流水线失败上下文输出诊断结论、证据引用和处置建议。不要生成或执行修复流水线。\n\n要求：\n1. 先给出最可能失败原因。\n2. 每个结论引用 evidence_index 中的证据 ref。\n3. 输出 JSON 结构化报告和 Markdown 正文。\n\n上下文：\n{diagnosis_context}',
                'report_schema': {
                    'required_sections': ['summary', 'impact_scope', 'key_evidence', 'possible_causes', 'recommended_actions', 'risks', 'next_checks'],
                    'evidence_required': True,
                },
            },
        },
        {
            'code': 'ci_ansible_failure',
            'name': 'Ansible 节点失败诊断',
            'description': '聚焦流水线中的 Ansible 节点、Ansible 执行摘要和 TaskLog，定位主机级失败原因。',
            'content': {
                'target_type': 'ansible_execution',
                'context_collection': {
                    'pipeline_run': True,
                    'failed_nodes': True,
                    'node_logs': True,
                    'approval_records': True,
                    'related_alerts': True,
                    'service_logs': False,
                    'metrics': False,
                    'ansible_execution': True,
                    'ansible_task_logs': True,
                },
                'log_keywords': ['fatal', 'failed', 'unreachable', 'permission denied', 'timeout', 'changed=false', 'rc=', 'stderr'],
                'prompt_template': '{prefix}\n你是 AnsFlow SRE 诊断助手，请基于以下 Ansible 节点失败上下文输出诊断结论、证据引用和处置建议。不要执行 SSH 或 Ansible 命令。\n\n请特别关注失败主机、TaskLog、返回码、权限/连通性/幂等性问题。\n\n上下文：\n{diagnosis_context}',
                'report_schema': {
                    'required_sections': ['summary', 'impact_scope', 'key_evidence', 'possible_causes', 'recommended_actions', 'risks', 'next_checks'],
                    'evidence_required': True,
                },
            },
        },
        {
            'code': 'post_release_service_regression',
            'name': '发布后服务异常诊断',
            'description': '关联最近发布流水线、服务日志、指标和告警，判断发布后服务回归风险。',
            'content': {
                'target_type': 'service_regression',
                'context_collection': {
                    'pipeline_run': True,
                    'failed_nodes': True,
                    'node_logs': True,
                    'approval_records': True,
                    'related_alerts': True,
                    'service_logs': True,
                    'metrics': True,
                    'ansible_execution': False,
                },
                'log_keywords': ['error', 'exception', 'timeout', '5xx', 'oom', 'connection refused', 'regression'],
                'prompt_template': '{prefix}\n你是 AnsFlow SRE 诊断助手，请基于以下发布后服务异常上下文输出诊断结论、证据引用和处置建议。第一版只提供建议，不触发自动修复。\n\n请比较发布事件、告警、日志高亮信号和指标变化。\n\n上下文：\n{diagnosis_context}',
                'report_schema': {
                    'required_sections': ['summary', 'impact_scope', 'key_evidence', 'possible_causes', 'recommended_actions', 'risks', 'next_checks'],
                    'evidence_required': True,
                },
            },
        },
    ]
    for template in templates:
        DiagnosisTemplate.objects.update_or_create(
            scope='global',
            project=None,
            code=template['code'],
            defaults={
                'name': template['name'],
                'description': template['description'],
                'category': 'ci_cd',
                'content': template['content'],
                'is_builtin': True,
                'is_active': True,
            },
        )


class Migration(migrations.Migration):

    dependencies = [
        ('rbac_permission', '0021_add_project_asset_share'),
        ('sre_management', '0008_observabilitydatasource_field_mapping_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='DiagnosisTemplate',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('create_time', models.DateTimeField(auto_now_add=True, verbose_name='创建时间')),
                ('update_time', models.DateTimeField(auto_now=True, verbose_name='更新时间')),
                ('remark', models.TextField(blank=True, null=True, verbose_name='备注')),
                ('scope', models.CharField(choices=[('global', '全局'), ('project', '项目')], default='global', max_length=20, verbose_name='模板范围')),
                ('code', models.CharField(max_length=80, verbose_name='模板编码')),
                ('name', models.CharField(max_length=120, verbose_name='模板名称')),
                ('description', models.TextField(blank=True, null=True, verbose_name='模板描述')),
                ('category', models.CharField(choices=[('ci_cd', 'CI/CD 发布诊断')], default='ci_cd', max_length=30, verbose_name='诊断分类')),
                ('content', models.JSONField(blank=True, default=dict, verbose_name='模板内容')),
                ('is_builtin', models.BooleanField(default=False, verbose_name='是否内置')),
                ('is_active', models.BooleanField(default=True, verbose_name='是否启用')),
                ('project', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='diagnosis_templates', to='rbac_permission.project', verbose_name='所属项目')),
            ],
            options={
                'verbose_name': '诊断模板',
                'verbose_name_plural': '诊断模板',
                'db_table': 'sre_diagnosis_template',
                'ordering': ['scope', 'project_id', 'code'],
                'unique_together': {('scope', 'project', 'code')},
            },
        ),
        migrations.AddField(
            model_name='diagnosisrun',
            name='template',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='diagnosis_runs', to='sre_management.diagnosistemplate', verbose_name='诊断模板'),
        ),
        migrations.RunPython(seed_builtin_templates, migrations.RunPython.noop),
    ]
