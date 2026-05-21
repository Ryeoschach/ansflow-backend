import os
import django
import subprocess

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'AnsFlow.settings')
django.setup()

from apps.k8s_management.models import K8sCluster
from apps.k8s_management.views.helm_views import HelmViewSet

c = K8sCluster.objects.get(id=2)
kube_path = HelmViewSet()._get_temp_kubeconfig(c)

print("Kubeconfig:", kube_path)
res = subprocess.run(['helm', 'list', '-n', 'default', '--kubeconfig', kube_path], capture_output=True, text=True)
print("Helm list default namespace:")
print(res.stdout)
print(res.stderr)

releases = subprocess.run(['helm', 'ls', '-n', 'default', '--all', '--kubeconfig', kube_path], capture_output=True, text=True)
print("All releases:")
print(releases.stdout)

# Attempt uninstall
un_res = subprocess.run(['helm', 'uninstall', 'my-test', '-n', 'default', '--kubeconfig', kube_path], capture_output=True, text=True)
print("Uninstall result:")
print(un_res.stdout)
print(un_res.stderr)

