from rest_framework import serializers
from .models import ApprovalPolicy, ApprovalTicket

class ApprovalPolicySerializer(serializers.ModelSerializer):
    class Meta:
        model = ApprovalPolicy
        fields = '__all__'

class ApprovalTicketSerializer(serializers.ModelSerializer):
    submitter_name = serializers.CharField(source='submitter.username', read_only=True)
    approver_name = serializers.CharField(source='approver.username', read_only=True)

    class Meta:
        model = ApprovalTicket
        fields = '__all__'
