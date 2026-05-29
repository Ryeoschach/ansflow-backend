from rest_framework import serializers
from django_celery_beat.models import PeriodicTask, IntervalSchedule, CrontabSchedule
import json

class IntervalScheduleSerializer(serializers.ModelSerializer):
    class Meta:
        model = IntervalSchedule
        fields = '__all__'

class CrontabScheduleSerializer(serializers.ModelSerializer):
    timezone = serializers.SerializerMethodField()

    class Meta:
        model = CrontabSchedule
        fields = '__all__'

    def get_timezone(self, obj):
        return str(obj.timezone) if obj.timezone else None

class PeriodicTaskSerializer(serializers.ModelSerializer):
    interval_detail = IntervalScheduleSerializer(source='interval', read_only=True)
    crontab_detail = CrontabScheduleSerializer(source='crontab', read_only=True)
    
    # 将 args 和 kwargs 转为友好的 JSON 格式
    args = serializers.CharField(required=False, allow_blank=True)
    kwargs = serializers.CharField(required=False, allow_blank=True)

    class Meta:
        model = PeriodicTask
        exclude = ('clocked', 'solar')  # 一般不用这些

    def validate_args(self, value):
        if value:
            try:
                json.loads(value)
            except ValueError:
                raise serializers.ValidationError("Args 必须是合法的 JSON 列表")
        return value

    def validate_kwargs(self, value):
        if value:
            try:
                json.loads(value)
            except ValueError:
                raise serializers.ValidationError("Kwargs 必须是合法的 JSON 对象")
        return value


from .models import UserNotification

class UserNotificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserNotification
        fields = '__all__'
