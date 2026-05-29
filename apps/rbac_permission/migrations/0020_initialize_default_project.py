from django.db import migrations

def create_default_project(apps, schema_editor):
    Project = apps.get_model('rbac_permission', 'Project')
    ProjectMember = apps.get_model('rbac_permission', 'ProjectMember')
    User = apps.get_model('rbac_permission', 'User')
    
    # Create default project
    default_owner = User.objects.filter(is_superuser=True).first()
    default_project, created = Project.objects.get_or_create(
        code='default',
        defaults={
            'name': '默认项目',
            'description': '系统初始化默认项目，所有未分类资源归属此处。',
            'owner': default_owner,
        }
    )
    
    # Bind existing superusers to default project as admins
    if default_project:
        for user in User.objects.filter(is_superuser=True):
            ProjectMember.objects.get_or_create(
                project=default_project,
                user=user,
                defaults={'role': 'admin'}
            )
            
    # Update other resources to point to the default project
    models_to_update = [
        ('credentials_management', 'Credential'),
        ('host_management', 'Host'),
        ('host_management', 'ResourcePool'),
        ('host_management', 'SshCredential'),
        ('pipeline_management', 'Pipeline'),
        ('k8s_management', 'K8sCluster'),
        ('task_management', 'AnsibleTask'),
    ]
    
    for app_label, model_name in models_to_update:
        try:
            Model = apps.get_model(app_label, model_name)
            Model.objects.filter(project__isnull=True).update(project=default_project)
        except LookupError:
            pass

def reverse_default_project(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('rbac_permission', '0019_project_projectmember'),
        ('credentials_management', '0002_credential_project'),
        ('host_management', '0012_host_project_resourcepool_project_and_more'),
        ('k8s_management', '0006_k8scluster_project'),
        ('pipeline_management', '0017_pipeline_project'),
        ('task_management', '0008_ansibletask_project'),
    ]

    operations = [
        migrations.RunPython(create_default_project, reverse_code=reverse_default_project),
    ]
