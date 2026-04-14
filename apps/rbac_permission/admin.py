from django.contrib import admin

from apps.rbac_permission.models import User, Permission, Role, Menu

# Register your models here.
admin.site.register(User)
admin.site.register(Permission)
admin.site.register(Role)
admin.site.register(Menu)