from django.core.management.base import BaseCommand
from apps.rbac_permission.models import Menu

class Command(BaseCommand):
    help = "清空所有菜单数据"

    def handle(self, *args, **options):
        count = Menu.objects.count()
        Menu.objects.all().delete()
        self.stdout.write(self.style.SUCCESS(f"成功清空 {count} 条菜单数据"))
