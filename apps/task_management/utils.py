import json
import os
from apps.host_management.models import ResourcePool

def generate_ansible_inventory(resource_pool_id):
    """
    根据资源池生成 Ansible 动态 Inventory 格式的字典。
    采用全兼容结构：将所有变量直接放入 hosts 字典中，避免 _meta 组解析歧义。
    Todo：增加资产的模版配置
    """
    try:
        pool = ResourcePool.objects.prefetch_related(
            'hosts', 'hosts__platform', 'hosts__credential', 'hosts__platform__default_credential'
        ).get(id=resource_pool_id)
        hosts = pool.hosts.all()
        
        # 组名增加前缀避免与主机名冲突
        group_key = f"pool_{pool.code}"
        
        # 构造 hosts 字典，key 为主机名，value 为该主机的变量
        inventory_hosts = {}
        for h in hosts:
            h_name = h.hostname
            
            # 基础变量
            h_vars = {
                "ansible_host": h.private_ip or h.ip_address,
                "ansible_port": int(h.ports) if h.ports and h.ports.isdigit() else 22,
                "ansible_connection": "ssh", # 强制 SSH
            }
            
            # 凭据逻辑：主机特定凭据 > 平台默认凭据
            cred = h.credential or (h.platform.default_credential if h.platform else None)
            
            if cred:
                h_vars["ansible_user"] = cred.username
                if cred.auth_type == 'password':
                    h_vars["ansible_ssh_pass"] = cred.password
                elif cred.auth_type == 'key':
                    # 标记为 key 认证，后续在 tasks.py 中处理临时文件
                    h_vars["_auth_type"] = "key"
                    h_vars["_private_key"] = cred.private_key
            
            inventory_hosts[h_name] = h_vars
        
        inventory = {
            "all": {
                "children": {
                    group_key: {
                        "hosts": inventory_hosts,
                        "vars": {
                            "pool_name": pool.name,
                            "pool_code": pool.code
                        }
                    }
                }
            }
        }
        
        return inventory
    except ResourcePool.DoesNotExist:
        return {"all": {"hosts": {}}}
