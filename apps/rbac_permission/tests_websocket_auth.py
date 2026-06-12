from django.core.cache import cache
from django.test import TestCase, override_settings
from unittest.mock import patch

from apps.k8s_management.models import K8sCluster
from apps.pipeline_management.models import Pipeline, PipelineRun
from apps.rbac_permission.models import (
    DataPolicy,
    Permission,
    Project,
    ProjectMember,
    Role,
    User,
)
from utils.websocket_auth import (
    _authorize_k8s_cluster,
    _authorize_pipeline_run,
)


@override_settings(
    CACHES={
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        }
    }
)
class WebSocketAuthorizationTestCase(TestCase):
    def setUp(self):
        signal_task_patcher = patch("apps.rbac_permission.signals.dispatch_refresh")
        signal_task_patcher.start()
        self.addCleanup(signal_task_patcher.stop)
        cache.clear()
        self.user = User.objects.create_user(username="ws-user", password="password")
        self.other_user = User.objects.create_user(
            username="ws-other",
            password="password",
        )
        self.project = Project.objects.create(
            name="WS Project",
            code="ws-project",
            owner=self.user,
        )
        self.other_project = Project.objects.create(
            name="Other Project",
            code="other-project",
            owner=self.other_user,
        )
        ProjectMember.objects.create(
            project=self.project,
            user=self.user,
            role="member",
        )
        ProjectMember.objects.create(
            project=self.other_project,
            user=self.user,
            role="member",
        )

        self.role = Role.objects.create(name="WS Role", code="ws-role")
        self.role.permissions.add(
            Permission.objects.create(
                name="View pipeline runs",
                code="pipeline:run:view",
                module="pipeline",
            ),
            Permission.objects.create(
                name="View K8s resources",
                code="k8s:cluster:resources_view",
                module="k8s",
            ),
            Permission.objects.create(
                name="Execute in pods",
                code="k8s:cluster:pod_exec",
                module="k8s",
            ),
        )
        self.user.roles.add(self.role)

        self.pipeline = Pipeline.objects.create(
            name="WS Pipeline",
            graph_data={},
            creator=self.user,
            project=self.project,
        )
        self.run = PipelineRun.objects.create(
            pipeline=self.pipeline,
            trigger_user=self.other_user,
        )
        self.cluster = K8sCluster.objects.create(
            name="WS Cluster",
            project=self.project,
            auth_type="token",
            api_server="https://k8s.example.com",
            token="fake-token",
        )

        DataPolicy.objects.create(
            role=self.role,
            resource_type="pipeline",
            action_type="use",
            authorized_ids=[self.pipeline.id],
        )
        DataPolicy.objects.create(
            role=self.role,
            resource_type="k8s_cluster",
            action_type="use",
            authorized_ids=[self.cluster.id],
        )

    def test_pipeline_run_requires_matching_project_and_data_scope(self):
        self.assertTrue(
            _authorize_pipeline_run(self.user, self.run.id, self.project.id)
        )
        self.assertFalse(
            _authorize_pipeline_run(self.user, self.run.id, self.other_project.id)
        )

    def test_pipeline_run_owner_can_subscribe_without_data_policy(self):
        self.run.trigger_user = self.user
        self.run.save(update_fields=["trigger_user"])
        DataPolicy.objects.filter(
            role=self.role,
            resource_type="pipeline",
        ).delete()
        cache.clear()

        self.assertTrue(
            _authorize_pipeline_run(self.user, self.run.id, self.project.id)
        )

    def test_k8s_cluster_requires_functional_and_data_permissions(self):
        self.assertEqual(
            _authorize_k8s_cluster(
                self.user,
                self.cluster.id,
                self.project.id,
                "k8s:cluster:pod_exec",
            ),
            self.cluster,
        )

        self.role.permissions.clear()
        cache.clear()
        self.assertIsNone(
            _authorize_k8s_cluster(
                self.user,
                self.cluster.id,
                self.project.id,
                "k8s:cluster:pod_exec",
            )
        )

    def test_superuser_can_access_resources_without_project_membership(self):
        admin = User.objects.create_superuser(
            username="ws-admin",
            password="password",
            email="admin@example.com",
        )
        self.assertTrue(_authorize_pipeline_run(admin, self.run.id))
        self.assertEqual(
            _authorize_k8s_cluster(admin, self.cluster.id),
            self.cluster,
        )
