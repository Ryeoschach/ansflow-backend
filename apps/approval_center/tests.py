from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.approval_center.engine import ProxyApprovalEngine
from apps.approval_center.models import ApprovalPolicy, ApprovalTicket
from apps.approval_center.views import user_matches_approver_roles
from apps.rbac_permission.models import Project, Role, User


@override_settings(
    CACHES={
        'default': {
            'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        }
    }
)
class ApprovalCenterTestCase(TestCase):
    def setUp(self):
        dispatch_patcher = patch('apps.rbac_permission.signals.dispatch_refresh')
        dispatch_patcher.start()
        self.addCleanup(dispatch_patcher.stop)
        result_notifier_patcher = patch(
            'apps.system_management.notifiers.notify_approval_result'
        )
        result_notifier_patcher.start()
        self.addCleanup(result_notifier_patcher.stop)

        self.submitter = User.objects.create_user(
            username='approval-submitter',
            password='password',
        )
        self.approver = User.objects.create_superuser(
            username='approval-admin',
            password='password',
            email='approval@example.com',
        )
        self.project = Project.objects.create(
            name='Approval Project',
            code='approval-project',
            owner=self.submitter,
        )
        self.policy = ApprovalPolicy.objects.create(
            name='Pipeline Approval',
            resource_type='pipeline:run',
            approval_timeout_minutes=30,
        )

    def make_request(self):
        request = MagicMock()
        request.user = self.submitter
        request.project = self.project
        request.data = {'release': 'v1.0.0'}
        request.method = 'POST'
        request._is_approved_execution = False
        request.get_full_path.return_value = '/api/v1/pipelines/1/execute/'
        return request

    @patch('apps.system_management.notifiers.notify_approval_requested')
    def test_intercept_deduplicates_active_ticket(self, notify_requested):
        first_blocked, first_response = ProxyApprovalEngine.intercept_if_needed(
            self.make_request(),
            resource_type='pipeline:run',
            target_id='1',
        )
        second_blocked, second_response = ProxyApprovalEngine.intercept_if_needed(
            self.make_request(),
            resource_type='pipeline:run',
            target_id='1',
        )

        self.assertTrue(first_blocked)
        self.assertTrue(second_blocked)
        self.assertEqual(first_response.data['ticket_id'], second_response.data['ticket_id'])
        self.assertTrue(second_response.data['duplicate'])
        self.assertEqual(ApprovalTicket.objects.count(), 1)
        notify_requested.assert_called_once()

    @patch('apps.system_management.notifiers.notify_approval_requested')
    def test_intercept_replaces_expired_ticket(self, notify_requested):
        _, first_response = ProxyApprovalEngine.intercept_if_needed(
            self.make_request(),
            resource_type='pipeline:run',
            target_id='1',
        )
        first_ticket = ApprovalTicket.objects.get(
            pk=first_response.data['ticket_id']
        )
        first_ticket.expires_at = timezone.now() - timedelta(minutes=1)
        first_ticket.save(update_fields=['expires_at'])

        _, second_response = ProxyApprovalEngine.intercept_if_needed(
            self.make_request(),
            resource_type='pipeline:run',
            target_id='1',
        )

        first_ticket.refresh_from_db()
        self.assertEqual(first_ticket.status, 'canceled')
        self.assertNotEqual(
            first_response.data['ticket_id'],
            second_response.data['ticket_id'],
        )
        self.assertFalse(second_response.data['duplicate'])
        self.assertEqual(notify_requested.call_count, 2)

    def test_policy_approver_roles_are_enforced(self):
        required_role = Role.objects.create(name='Release Approver', code='release-approver')
        other_role = Role.objects.create(name='Other Role', code='other-role')
        self.policy.approver_roles.add(required_role)
        self.submitter.roles.add(other_role)

        self.assertFalse(
            user_matches_approver_roles(self.submitter, self.policy)
        )
        self.submitter.roles.add(required_role)
        self.assertTrue(
            user_matches_approver_roles(self.submitter, self.policy)
        )

    @patch('apps.approval_center.views.ProxyApprovalEngine.resume_execution')
    def test_approve_claims_ticket_before_execution(self, resume_execution):
        ticket = ApprovalTicket.objects.create(
            title='Approve once',
            submitter=self.submitter,
            policy=self.policy,
            project=self.project,
            resource_type='pipeline:run',
            target_id='1',
            payload={},
            url_path='/api/v1/pipelines/1/execute/',
            method='POST',
            expires_at=timezone.now() + timedelta(minutes=30),
        )
        resume_execution.side_effect = lambda item, user: ApprovalTicket.objects.filter(
            pk=item.pk
        ).update(status='finished')
        client = APIClient()
        client.force_authenticate(self.approver)
        url = reverse('approval_tickets-approve', args=[ticket.id])

        first_response = client.post(url)
        second_response = client.post(url)

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(second_response.status_code, 400)
        resume_execution.assert_called_once()

    @patch('apps.approval_center.views.ProxyApprovalEngine.resume_execution')
    def test_expired_ticket_cannot_execute(self, resume_execution):
        ticket = ApprovalTicket.objects.create(
            title='Expired approval',
            submitter=self.submitter,
            policy=self.policy,
            project=self.project,
            resource_type='pipeline:run',
            payload={},
            url_path='/api/v1/pipelines/1/execute/',
            method='POST',
            expires_at=timezone.now() - timedelta(minutes=1),
        )
        client = APIClient()
        client.force_authenticate(self.approver)

        response = client.post(
            reverse('approval_tickets-approve', args=[ticket.id])
        )

        self.assertEqual(response.status_code, 400)
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, 'canceled')
        resume_execution.assert_not_called()
