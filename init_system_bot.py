import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.local') # 根据实际情况调整
django.setup()

from apps.rbac_permission.models import User

def init_system_bot():
    username = 'system_bot'
    if not User.objects.filter(username=username).exists():
        User.objects.create_user(
            username=username,
            password='SystemBotPassword!2026', # 随机密码
            first_name='System',
            last_name='Bot',
            email='bot@ansflow.local',
            is_staff=False,
            is_active=True,
            remark="Created by Gemini CLI for SRE Self-healing Approval Integration. Inspired by User: Creed."
        )
        print(f"User {username} created successfully.")
    else:
        print(f"User {username} already exists.")

if __name__ == "__main__":
    init_system_bot()
