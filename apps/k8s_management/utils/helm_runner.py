import subprocess
import os
import tempfile
import yaml
import json
import shutil
from django.conf import settings

def get_temp_kubeconfig(cluster):
    """
    Todo: 目录统一配置
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

def get_persistent_chart_path(chart_name):
    """
    获取持久化 Chart 的物理路径 (media/helm_charts/)
    """
    base_dir = getattr(settings, 'MEDIA_ROOT', 
                        os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'media'))
    charts_dir = os.path.join(base_dir, 'helm_charts')
    if not os.path.exists(charts_dir):
        os.makedirs(charts_dir, exist_ok=True)
    # 如果已经有 .tgz 后缀，就不重复加
    filename = chart_name if chart_name.endswith('.tgz') else f"{chart_name}.tgz"
    return os.path.join(charts_dir, filename)

def run_helm_upgrade(cluster, name, namespace='default', chart=None, values=None, force=False, version=None, repo_url=None, repo_auth=None):
    """
    执行 Helm 升级/部署逻辑 (增强版：支持远程拉取)
    :param repo_auth: {'username': 'xxx', 'password': 'xxx'}
    """
    kubeconfig_path = get_temp_kubeconfig(cluster)
    temp_val_path = None
    
    # --- 并行隔离设计 ---
    # 为 Helm 创建独立的临时家目录，防止并发执行时的锁冲突
    helm_home = tempfile.mkdtemp(prefix='helm-home-')
    env = os.environ.copy()
    env.update({
        'HELM_CACHE_HOME': os.path.join(helm_home, 'cache'),
        'HELM_CONFIG_HOME': os.path.join(helm_home, 'config'),
        'HELM_DATA_HOME': os.path.join(helm_home, 'data'),
    })

    try:
        # 1. 处理 Chart 来源
        target_chart = chart
        
        # 如果提供了仓库地址，先拉取
        if repo_url:
            repo_name = f"repo-{hash(repo_url)}"
            add_cmd = ['helm', 'repo', 'add', repo_name, repo_url, '--kubeconfig', kubeconfig_path]
            if repo_auth:
                add_cmd.extend(['--username', repo_auth['username'], '--password', repo_auth['password']])
            
            subprocess.run(add_cmd, env=env, capture_output=True)
            subprocess.run(['helm', 'repo', 'update', repo_name, '--kubeconfig', kubeconfig_path], env=env, capture_output=True)
            
            # target_chart 变为 repo/chart_name
            target_chart = f"{repo_name}/{chart}"

        elif chart:
            # 兼容旧逻辑：检查本地 media 目录
            p_path = get_persistent_chart_path(chart)
            if os.path.exists(p_path):
                target_chart = p_path
        
        if not target_chart:
             return False, "未能定位到有效的 Chart 来源 (本地文件或远程仓库)"

        # 2. 构造部署命令
        cmd = ['helm', 'upgrade', name, target_chart, '-n', namespace, '--kubeconfig', kubeconfig_path, '--install']
        
        if values:
            fd, temp_val_path = tempfile.mkstemp(suffix='.yaml')
            with os.fdopen(fd, 'w') as f:
                f.write(values)
            cmd.extend(['-f', temp_val_path])
        
        if force:
            cmd.extend(['--server-side=true', '--force-conflicts'])
            
        if version:
            cmd.extend(['--version', str(version)])

        # 3. 执行
        result = subprocess.run(cmd, env=env, capture_output=True, text=True)
        if result.returncode != 0:
            return False, result.stderr or result.stdout
        
        return True, result.stdout
        
    except Exception as e:
        return False, str(e)
    finally:
        # 4. 强力清理
        if os.path.exists(kubeconfig_path): os.remove(kubeconfig_path)
        if temp_val_path and os.path.exists(temp_val_path): os.remove(temp_val_path)
        shutil.rmtree(helm_home, ignore_errors=True)
