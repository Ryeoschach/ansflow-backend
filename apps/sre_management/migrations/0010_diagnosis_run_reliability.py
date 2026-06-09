from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('sre_management', '0009_diagnosistemplate_diagnosisrun_template'),
    ]

    operations = [
        migrations.AlterUniqueTogether(
            name='diagnosistemplate',
            unique_together=set(),
        ),
        migrations.AddConstraint(
            model_name='diagnosistemplate',
            constraint=models.UniqueConstraint(
                condition=models.Q(scope='global'),
                fields=('code',),
                name='uniq_global_diagnosis_template_code',
            ),
        ),
        migrations.AddConstraint(
            model_name='diagnosistemplate',
            constraint=models.UniqueConstraint(
                condition=models.Q(scope='project'),
                fields=('project', 'code'),
                name='uniq_project_diagnosis_template_code',
            ),
        ),
        migrations.AddField(
            model_name='diagnosisrun',
            name='attempt_count',
            field=models.PositiveIntegerField(default=0, verbose_name='执行尝试次数'),
        ),
        migrations.AddField(
            model_name='diagnosisrun',
            name='celery_task_id',
            field=models.CharField(blank=True, db_index=True, max_length=128, null=True, verbose_name='Celery 任务 ID'),
        ),
        migrations.AddField(
            model_name='diagnosisrun',
            name='heartbeat_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='任务心跳时间'),
        ),
        migrations.AddIndex(
            model_name='diagnosisrun',
            index=models.Index(fields=['project', '-create_time'], name='sre_diag_project_created_idx'),
        ),
        migrations.AddIndex(
            model_name='diagnosisrun',
            index=models.Index(fields=['status', '-create_time'], name='sre_diag_status_created_idx'),
        ),
        migrations.AddIndex(
            model_name='diagnosisrun',
            index=models.Index(fields=['diagnosis_time'], name='sre_diag_time_idx'),
        ),
    ]
