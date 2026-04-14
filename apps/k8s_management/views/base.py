import os
import tempfile
import yaml
from django.conf import settings

class K8sBaseMixin:
    """
    提供 K8s 和 Helm 视图通用的工具方法
    """

    def _get_temp_kubeconfig(self, cluster):
        """
        根据集群配置生成临时的 Kubeconfig 文件路径
        """
        f = tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False)
        if cluster.auth_type == 'kubeconfig':
            f.write(cluster.kubeconfig_content)
        else:
            kcfg = {
                'apiVersion': 'v1',
                'clusters': [{'cluster': {'insecure-skip-tls-verify': True, 'server': cluster.api_server},
                              'name': 'temp-cluster'}],
                'contexts': [{'context': {'cluster': 'temp-cluster', 'user': 'temp-user'}, 'name': 'temp-context'}],
                'current-context': 'temp-context',
                'kind': 'Config',
                'users': [{'name': 'temp-user', 'user': {'token': cluster.token}}]
            }
            yaml.dump(kcfg, f)
        f.close()
        return f.name

    def _get_persistent_chart_path(self, chart_name):
        """
        获取持久化 Chart 的物理路径 (media/helm_charts/)
        """
        base_dir = getattr(settings, 'MEDIA_ROOT',
                           os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
        charts_dir = os.path.join(base_dir, 'helm_charts')
        if not os.path.exists(charts_dir):
            os.makedirs(charts_dir, exist_ok=True)
        return os.path.join(charts_dir, f"{chart_name}.tgz")
