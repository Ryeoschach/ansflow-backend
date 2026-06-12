from urllib.parse import parse_qs

from channels.db import database_sync_to_async


def get_query_param(scope, name):
    query_string = scope.get("query_string", b"").decode("utf-8")
    return parse_qs(query_string).get(name, [None])[0]


@database_sync_to_async
def authenticate_websocket(scope):
    token = get_query_param(scope, "token")
    if token:
        try:
            from django.contrib.auth import get_user_model
            from rest_framework_simplejwt.tokens import AccessToken

            user_id = AccessToken(token)["user_id"]
            return get_user_model().objects.filter(id=user_id, is_active=True).first()
        except Exception:
            return None

    user = scope.get("user")
    if user and user.is_authenticated and user.is_active:
        return user
    return None


def _has_permission(user, required_code):
    if user.is_superuser:
        return True

    permission_codes = set()
    for role in user.roles.all():
        permission_codes.update(
            role.get_all_permissions().values_list("code", flat=True)
        )

    if "*" in permission_codes or required_code in permission_codes:
        return True

    parts = required_code.split(":")
    return any(
        f"{':'.join(parts[:index])}:*" in permission_codes
        for index in range(1, len(parts))
    )


def _resolve_project(user, project_id, fallback_project=None):
    from apps.rbac_permission.models import Project, ProjectMember

    project = None
    if project_id and str(project_id).isdigit():
        project = Project.objects.filter(id=project_id).first()
    if not project:
        project = fallback_project
    if not project:
        return None
    if user.is_superuser:
        return project
    if ProjectMember.objects.filter(project=project, user=user).exists():
        return project
    return None


def _is_shared_to_project(project, asset_type, asset_id):
    from apps.rbac_permission.models import ProjectAssetShare

    return ProjectAssetShare.objects.filter(
        to_project=project,
        asset_type=asset_type,
        asset_id=asset_id,
    ).exists()


def _is_in_data_scope(user, resource_type, resource_id):
    from utils.rbac_permission import get_user_data_scope

    allowed_ids = get_user_data_scope(user, resource_type, action_type="use")
    normalized_ids = {
        int(value) for value in allowed_ids if str(value).isdigit()
    }
    return "*" in allowed_ids or resource_id in normalized_ids


def _authorize_pipeline_run(user, run_id, project_id=None):
    from apps.pipeline_management.models import PipelineRun

    if not user or not _has_permission(user, "pipeline:run:view"):
        return False

    run = (
        PipelineRun.objects.select_related("pipeline__project", "trigger_user")
        .filter(id=run_id)
        .first()
    )
    if not run:
        return False
    if user.is_superuser:
        return True

    project = _resolve_project(user, project_id, run.pipeline.project)
    if not project:
        return False

    belongs_to_project = run.pipeline.project_id == project.id
    is_shared = _is_shared_to_project(project, "pipeline", run.pipeline_id)
    if not belongs_to_project and not is_shared:
        return False

    return run.trigger_user_id == user.id or _is_in_data_scope(
        user, "pipeline", run.pipeline_id
    )


@database_sync_to_async
def authorize_pipeline_run(user, run_id, project_id=None):
    return _authorize_pipeline_run(user, run_id, project_id)


@database_sync_to_async
def authorize_pipeline_stream(user, project_id):
    if not user or not _has_permission(user, "pipeline:run:view"):
        return None
    project = _resolve_project(user, project_id)
    return project.id if project else None


@database_sync_to_async
def authorize_pipeline_event(user, run_id, project_id):
    return _authorize_pipeline_run(user, run_id, project_id)


def _authorize_k8s_cluster(
    user,
    cluster_id,
    project_id=None,
    permission_code="k8s:cluster:view",
):
    from apps.k8s_management.models import K8sCluster

    if not user or not _has_permission(user, permission_code):
        return None

    cluster = K8sCluster.objects.select_related("project").filter(id=cluster_id).first()
    if not cluster:
        return None
    if user.is_superuser:
        return cluster

    project = _resolve_project(user, project_id, cluster.project)
    if not project:
        return None

    belongs_to_project = cluster.project_id == project.id
    is_shared = _is_shared_to_project(project, "k8s_cluster", cluster.id)
    if not belongs_to_project and not is_shared:
        return None
    if not _is_in_data_scope(user, "k8s_cluster", cluster.id):
        return None
    return cluster


@database_sync_to_async
def authorize_k8s_cluster(
    user,
    cluster_id,
    project_id=None,
    permission_code="k8s:cluster:view",
):
    return _authorize_k8s_cluster(
        user,
        cluster_id,
        project_id,
        permission_code,
    )
