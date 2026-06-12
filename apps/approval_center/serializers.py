from rest_framework import serializers
from .models import ApprovalPolicy, ApprovalTicket, ApprovalResource
from apps.rbac_permission.serializers import RoleSerializer

class ApprovalResourceSerializer(serializers.ModelSerializer):
    class Meta:
        model = ApprovalResource
        fields = '__all__'

class ApprovalPolicySerializer(serializers.ModelSerializer):
    approver_roles_detail = RoleSerializer(source='approver_roles', many=True, read_only=True)
    
    class Meta:
        model = ApprovalPolicy
        fields = '__all__'

class ApprovalTicketSerializer(serializers.ModelSerializer):
    submitter_name = serializers.CharField(source='submitter.username', read_only=True)
    approver_name = serializers.CharField(source='approver.username', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    policy_name = serializers.CharField(source='policy.name', read_only=True)
    project_name = serializers.CharField(source='project.name', read_only=True)

    class Meta:
        model = ApprovalTicket
        fields = '__all__'
