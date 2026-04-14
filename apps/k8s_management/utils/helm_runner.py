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

def run_helm_upgrade(cluster, name, namespace='default', chart=None, values=None, force=False, version=None):
    """
    执行 Helm 升级/部署逻辑
    """
    kubeconfig_path = get_temp_kubeconfig(cluster)
    temp_val_path = None
    
    try:
        # 如果指定了 Chart 名，检查是否是 media 目录下的本地文件
        if chart:
            p_path = get_persistent_chart_path(chart)
            if os.path.exists(p_path):
                chart = p_path
                
        # 如果没有指定 Chart，尝试从现有的 Release 中自动探测
        if not chart:
            list_cmd = ['helm', 'list', '-n', namespace, '--filter', f'^{name}$', '--output', 'json', '--kubeconfig', kubeconfig_path]
            list_res = subprocess.run(list_cmd, capture_output=True, text=True)
            if list_res.returncode == 0:
                releases = json.loads(list_res.stdout)
                if releases:
                    c_fullname = releases[0].get('chart')
                    p_path = get_persistent_chart_path(c_fullname)
                    chart = p_path if os.path.exists(p_path) else None
        
        if not chart:
            return False, "未能定位到该 Release 的 Chart 存储副本。如果您是通过在线编辑器刚创建的 Chart（或者是旧版本数据），由于以前并未持久化保存该 Chart 的物理包，导致自动匹配失败。\n解决办法：请前往【应用发布 - K8s 资源中心】重新创建一个新的应用，然后再到流水线配置节点明确选中这个 Chart 运行即可发布更新。"

        cmd = ['helm', 'upgrade', name, chart, '-n', namespace, '--kubeconfig', kubeconfig_path, '--install']
        
        if values:
            fd, temp_val_path = tempfile.mkstemp(suffix='.yaml')
            with os.fdopen(fd, 'w') as f:
                f.write(values)
            cmd.extend(['-f', temp_val_path])
        
        if force:
            cmd.extend(['--server-side=true', '--force-conflicts'])
            
        if version:
            cmd.extend(['--version', str(version)])
            
        # 为了应对一些变动，增加更多的选项，比如等待部署完成
        # cmd.append('--wait') 

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            return False, result.stderr or result.stdout
        
        return True, result.stdout
        
    except Exception as e:
        return False, str(e)
    finally:
        if os.path.exists(kubeconfig_path):
            os.remove(kubeconfig_path)
        if temp_val_path and os.path.exists(temp_val_path):
            os.remove(temp_val_path)
