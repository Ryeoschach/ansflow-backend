from django.db import migrations, models
import django.db.models.deletion


def seed_runtime_templates(apps, schema_editor):
    DiagnosisTemplate = apps.get_model('sre_management', 'DiagnosisTemplate')
    DiagnosisTemplateVersion = apps.get_model('sre_management', 'DiagnosisTemplateVersion')
    templates = [
        ('service_alert_diagnosis', '服务告警综合诊断', 'service', 'alert_service'),
        ('k8s_workload_failure', 'Kubernetes 工作负载异常诊断', 'kubernetes', 'k8s_workload'),
        ('host_runtime_failure', '主机运行异常诊断', 'host', 'host_runtime'),
        ('jvm_runtime_failure', 'JVM 应用异常诊断', 'jvm', 'jvm_runtime'),
    ]
    for code, name, category, target_type in templates:
        template, _ = DiagnosisTemplate.objects.update_or_create(
            scope='global',
            project=None,
            code=code,
            defaults={
                'name': name,
                'description': f'{name}，关联资产、日志、指标、告警和变更证据。',
                'category': category,
                'content': {
                    'target_type': target_type,
                    'context_collection': {
                        'service_logs': True,
                        'metrics': True,
                        'related_alerts': True,
                        'runtime_assets': True,
                        'k8s_runtime': target_type == 'k8s_workload',
                        'host_runtime': target_type == 'host_runtime',
                        'jvm_runtime': target_type == 'jvm_runtime',
                    },
                    'log_keywords': ['error', 'exception', 'timeout', 'oom', 'failed', 'unhealthy'],
                    'prompt_template': (
                        '{prefix}\n请基于以下运行时诊断上下文输出有证据支持的根因候选、'
                        '影响范围、处置建议、风险与验证步骤。不得执行任何命令。\n{diagnosis_context}'
                    ),
                    'report_schema': {'evidence_required': True, 'confidence_required': True},
                },
                'is_builtin': True,
                'is_active': True,
                'version': 1,
                'lifecycle_status': 'published',
            },
        )
        DiagnosisTemplateVersion.objects.get_or_create(
            template=template,
            version=1,
            defaults={
                'name': template.name,
                'description': template.description,
                'category': template.category,
                'content': template.content,
                'change_summary': 'Built-in runtime diagnosis template',
            },
        )

    for template in DiagnosisTemplate.objects.all():
        DiagnosisTemplateVersion.objects.get_or_create(
            template=template,
            version=template.version or 1,
            defaults={
                'name': template.name,
                'description': template.description,
                'category': template.category,
                'content': template.content,
                'change_summary': 'Initial version snapshot',
            },
        )


class Migration(migrations.Migration):

    dependencies = [
        ('sre_management', '0010_diagnosis_run_reliability'),
    ]

    operations = [
        migrations.AlterField(
            model_name='diagnosistemplate',
            name='category',
            field=models.CharField(
                choices=[
                    ('ci_cd', 'CI/CD 发布诊断'),
                    ('service', '服务运行诊断'),
                    ('kubernetes', 'Kubernetes 诊断'),
                    ('host', '主机运行诊断'),
                    ('jvm', 'JVM 应用诊断'),
                ],
                default='ci_cd',
                max_length=30,
                verbose_name='诊断分类',
            ),
        ),
        migrations.AddField(
            model_name='diagnosistemplate',
            name='lifecycle_status',
            field=models.CharField(
                choices=[('draft', '草稿'), ('published', '已发布'), ('deprecated', '已废弃')],
                default='published',
                max_length=20,
                verbose_name='生命周期状态',
            ),
        ),
        migrations.AddField(
            model_name='diagnosistemplate',
            name='version',
            field=models.PositiveIntegerField(default=1, verbose_name='当前版本'),
        ),
        migrations.AddField(
            model_name='diagnosisrun',
            name='confidence_score',
            field=models.FloatField(default=0, verbose_name='综合置信度'),
        ),
        migrations.AddField(
            model_name='diagnosisrun',
            name='evidence_coverage',
            field=models.FloatField(default=0, verbose_name='证据覆盖率'),
        ),
        migrations.AddField(
            model_name='diagnosisrun',
            name='quality_score',
            field=models.FloatField(default=0, verbose_name='诊断质量分'),
        ),
        migrations.CreateModel(
            name='DiagnosisTemplateVersion',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('create_time', models.DateTimeField(auto_now_add=True, verbose_name='创建时间')),
                ('update_time', models.DateTimeField(auto_now=True, verbose_name='更新时间')),
                ('remark', models.TextField(blank=True, null=True, verbose_name='备注')),
                ('version', models.PositiveIntegerField(verbose_name='版本号')),
                ('name', models.CharField(max_length=120, verbose_name='模板名称')),
                ('description', models.TextField(blank=True, null=True, verbose_name='模板描述')),
                ('category', models.CharField(choices=[('ci_cd', 'CI/CD 发布诊断'), ('service', '服务运行诊断'), ('kubernetes', 'Kubernetes 诊断'), ('host', '主机运行诊断'), ('jvm', 'JVM 应用诊断')], max_length=30)),
                ('content', models.JSONField(blank=True, default=dict, verbose_name='模板内容快照')),
                ('change_summary', models.CharField(blank=True, max_length=255, null=True, verbose_name='变更说明')),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='diagnosis_template_versions', to='rbac_permission.user')),
                ('template', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='versions', to='sre_management.diagnosistemplate', verbose_name='诊断模板')),
            ],
            options={'db_table': 'sre_diagnosis_template_version', 'ordering': ['-version']},
        ),
        migrations.AddConstraint(
            model_name='diagnosistemplateversion',
            constraint=models.UniqueConstraint(fields=('template', 'version'), name='uniq_diagnosis_template_version'),
        ),
        migrations.CreateModel(
            name='DiagnosisFeedback',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('create_time', models.DateTimeField(auto_now_add=True, verbose_name='创建时间')),
                ('update_time', models.DateTimeField(auto_now=True, verbose_name='更新时间')),
                ('remark', models.TextField(blank=True, null=True, verbose_name='备注')),
                ('accuracy_rating', models.PositiveSmallIntegerField(verbose_name='准确性评分')),
                ('evidence_rating', models.PositiveSmallIntegerField(verbose_name='证据有效性评分')),
                ('actionability_rating', models.PositiveSmallIntegerField(verbose_name='建议可执行性评分')),
                ('root_cause_correct', models.BooleanField(blank=True, null=True, verbose_name='根因是否正确')),
                ('recommendation_adopted', models.BooleanField(blank=True, null=True, verbose_name='建议是否采纳')),
                ('corrected_root_cause', models.TextField(blank=True, null=True, verbose_name='人工修正根因')),
                ('comment', models.TextField(blank=True, null=True, verbose_name='反馈备注')),
                ('run', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='feedbacks', to='sre_management.diagnosisrun', verbose_name='诊断任务')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='diagnosis_feedbacks', to='rbac_permission.user')),
            ],
            options={'db_table': 'sre_diagnosis_feedback', 'ordering': ['-create_time']},
        ),
        migrations.AddConstraint(
            model_name='diagnosisfeedback',
            constraint=models.UniqueConstraint(fields=('run', 'user'), name='uniq_diagnosis_feedback_run_user'),
        ),
        migrations.CreateModel(
            name='DiagnosisReplayCase',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('create_time', models.DateTimeField(auto_now_add=True, verbose_name='创建时间')),
                ('update_time', models.DateTimeField(auto_now=True, verbose_name='更新时间')),
                ('remark', models.TextField(blank=True, null=True, verbose_name='备注')),
                ('name', models.CharField(max_length=160, verbose_name='用例名称')),
                ('description', models.TextField(blank=True, null=True)),
                ('fixture_context', models.JSONField(default=dict, verbose_name='脱敏回放上下文')),
                ('expected', models.JSONField(blank=True, default=dict, verbose_name='预期根因、证据和阈值')),
                ('is_active', models.BooleanField(default=True)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='diagnosis_replay_cases', to='rbac_permission.user')),
                ('project', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='diagnosis_replay_cases', to='rbac_permission.project')),
                ('source_run', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='replay_cases', to='sre_management.diagnosisrun')),
                ('template', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='replay_cases', to='sre_management.diagnosistemplate')),
            ],
            options={'db_table': 'sre_diagnosis_replay_case', 'ordering': ['-create_time']},
        ),
        migrations.AddIndex(
            model_name='diagnosisreplaycase',
            index=models.Index(fields=['project', 'is_active'], name='sre_replay_project_active_idx'),
        ),
        migrations.CreateModel(
            name='DiagnosisReplayResult',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('create_time', models.DateTimeField(auto_now_add=True, verbose_name='创建时间')),
                ('update_time', models.DateTimeField(auto_now=True, verbose_name='更新时间')),
                ('remark', models.TextField(blank=True, null=True, verbose_name='备注')),
                ('template_version', models.PositiveIntegerField(blank=True, null=True)),
                ('status', models.CharField(choices=[('pending', '待执行'), ('running', '执行中'), ('passed', '通过'), ('failed', '失败')], default='pending', max_length=20)),
                ('score', models.FloatField(default=0)),
                ('passed', models.BooleanField(default=False)),
                ('structured_report', models.JSONField(blank=True, default=dict)),
                ('ai_result', models.TextField(blank=True, null=True)),
                ('evaluation', models.JSONField(blank=True, default=dict)),
                ('error_message', models.TextField(blank=True, null=True)),
                ('started_at', models.DateTimeField(blank=True, null=True)),
                ('finished_at', models.DateTimeField(blank=True, null=True)),
                ('case', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='results', to='sre_management.diagnosisreplaycase')),
                ('executed_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='diagnosis_replay_results', to='rbac_permission.user')),
            ],
            options={'db_table': 'sre_diagnosis_replay_result', 'ordering': ['-create_time']},
        ),
        migrations.RunPython(seed_runtime_templates, migrations.RunPython.noop),
    ]
