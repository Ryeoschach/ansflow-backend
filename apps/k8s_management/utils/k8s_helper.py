import yaml
from kubernetes import client, config

def get_k8s_client(cluster):
    """
    根据集群模型构建 ApiClient (统一走 Kubeconfig 逻辑以支持 WebSocket 连接)
    """
    from kubernetes import client, config
    import yaml

    if cluster.auth_type == 'kubeconfig':
        config_dict = yaml.safe_load(cluster.kubeconfig_content)
        return config.new_client_from_config_dict(config_dict)
    else:
        # 将 Token 模式转换成内存中的 Kubeconfig 格式，以便利用官方库更完善的握手处理
        kcfg = {
            'apiVersion': 'v1',
            'clusters': [{
                'cluster': {
                    'insecure-skip-tls-verify': True, 
                    'server': cluster.api_server
                }, 
                'name': 'temp-cluster'
            }],
            'contexts': [{
                'context': {
                    'cluster': 'temp-cluster', 
                    'user': 'temp-user'
                }, 
                'name': 'temp-context'}
            ],
            'current-context': 'temp-context',
            'kind': 'Config',
            'users': [{
                'name': 'temp-user', 
                'user': {'token': cluster.token}
            }]
        }
        return config.new_client_from_config_dict(kcfg)
