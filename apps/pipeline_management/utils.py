import re
import logging
from typing import Any, Dict
from django.db.models import QuerySet

logger = logging.getLogger(__name__)

def resolve_pipeline_vars(content: Any, run_id: int) -> Any:
    """
    解析流水线变量占位符。
    支持语法:
    - {{ nodes.NODE_ID.KEY }} : 引用特定节点的输出变量
    - {{ run_id }} : 当前运行 ID
    - {{ pipeline_name }} : 流水线名称
    """
    from .models import PipelineRun, PipelineNodeRun
    
    if content is None:
        return content

    # 如果是字典或列表，递归处理
    if isinstance(content, dict):
        return {k: resolve_pipeline_vars(v, run_id) for k, v in content.items()}
    if isinstance(content, list):
        return [resolve_pipeline_vars(i, run_id) for i in content]
    
    if not isinstance(content, str):
        return content

    if "{{" not in content:
        return content

    try:
        run = PipelineRun.objects.get(id=run_id)
        # 获取所有已成功的节点输出，建立索引
        # 使用 node_id 作为 key，方便快速查找
        success_nodes = PipelineNodeRun.objects.filter(run_id=run_id, status='success').exclude(output_data__isnull=True)
        context = {
            "run_id": str(run_id),
            "pipeline_name": run.pipeline.name,
            "nodes": {}
        }
        
        # 注入运行实例自带的全局变量 (如自愈触发时注入的 alert 信息)
        if run.extra_vars:
            context.update(run.extra_vars)
        
        for node in success_nodes:
            context["nodes"][node.node_id] = node.output_data

        # 正则替换函数
        def _replace(match):
            path = match.group(1).strip()
            # 路径示例: "nodes.dndnode_1.tag"
            parts = path.split('.')
            
            val = context
            for p in parts:
                if isinstance(val, dict) and p in val:
                    val = val[p]
                else:
                    return match.group(0) # 没找到，保持原样
            
            return str(val)

        # 匹配 {{ variable.path }}
        pattern = r"\{\{\s*([\w\.\-_]+)\s*\}\}"
        resolved = re.sub(pattern, _replace, content)
        return resolved

    except Exception as e:
        logger.error(f"解析流水线变量失败 [Run #{run_id}]: {str(e)}")
        return content


def normalize_and_filter_ai_dag(graph_data: dict) -> dict:
    """
    通用规范化并过滤 AI 自动生成的 DAG 流程图。
    处理包括：
    1. 边规范化 (from/to -> source/target)
    2. 类型规范化 (如 git -> git_clone, manual -> approval, trigger/event/start -> input)
    3. 初始化 data 字典并将 node.name 同步为 data.label
    4. 排除/过滤非动作节点 (如 input, approval，或名称/标签包含"接收告警"、"人工审批"等非动作节点)
    5. 旁路缝合被过滤节点的前后依赖边
    6. 计算拓扑层级并设置位置，避免画布节点重叠
    """
    if not isinstance(graph_data, dict):
        return graph_data
    
    nodes = graph_data.get('nodes', [])
    edges = graph_data.get('edges', [])
    
    # 1. 规范化边关系 (from/to -> source/target)
    normalized_edges = []
    for edge in edges:
        src = edge.get('source') or edge.get('from')
        tgt = edge.get('target') or edge.get('to')
        if src and tgt:
            edge['source'] = src
            edge['target'] = tgt
            edge.pop('from', None)
            edge.pop('to', None)
            normalized_edges.append(edge)
    edges = normalized_edges
    
    # 2. 规范化节点类型与初始化数据字典
    TYPE_MAPPING = {
        'manual': 'approval',
        'notification': 'http_webhook',
        'git-checkout': 'git_clone',
        'git': 'git_clone',
        'trigger': 'input',
        'start': 'input',
        'event': 'input',
        'task': 'ansible',
        'execute': 'ansible',
        'shell': 'ansible',
        'cmd': 'ansible',
        'playbook': 'ansible'
    }
    for node in nodes:
        node_type = node.get('type')
        if node_type in TYPE_MAPPING:
            node['type'] = TYPE_MAPPING[node_type]
        
        node_data = node.setdefault('data', {})
        if not isinstance(node_data, dict):
            node_data = {}
            node['data'] = node_data
        
        if 'name' in node and 'label' not in node_data:
            node_data['label'] = node['name']
            
    # 3. 过滤并旁路排除非系统组件
    EXCLUDED_TYPES = {'input', 'approval'}
    EXCLUDED_KEYWORDS = {'接收告警', '人工审批', '审批修复', '告警接收', '告警触发', '人工确认'}

    def is_excluded(n):
        n_type = n.get('type')
        if n_type in EXCLUDED_TYPES:
            return True
        n_name = n.get('name') or ''
        n_label = n.get('data', {}).get('label') or ''
        if any(kw in n_name or kw in n_label for kw in EXCLUDED_KEYWORDS):
            return True
        return False

    filtered_nodes = list(nodes)
    for node in list(filtered_nodes):
        if is_excluded(node):
            u = node.get('id')
            if not u:
                continue
            # 寻找流入和流出的节点
            incoming = [e['source'] for e in edges if e['target'] == u]
            outgoing = [e['target'] for e in edges if e['source'] == u]
            
            # 对每一个流入节点与流出节点建立直接连接
            for src in incoming:
                for tgt in outgoing:
                    if src != tgt:
                        if not any(e['source'] == src and e['target'] == tgt for e in edges):
                            edges.append({'source': src, 'target': tgt})
            
            # 删除与节点 u 关联的所有边
            edges = [e for e in edges if e['source'] != u and e['target'] != u]
            # 从节点列表中移除
            filtered_nodes.remove(node)
            
    nodes = filtered_nodes
    
    # 4. 计算拓扑层级并设置位置，避免画布节点重叠
    node_map = {n['id']: n for n in nodes if 'id' in n}
    adj = {n_id: [] for n_id in node_map}
    in_degree = {n_id: 0 for n_id in node_map}
    
    for edge in edges:
        src = edge.get('source')
        tgt = edge.get('target')
        if src in node_map and tgt in node_map:
            adj[src].append(tgt)
            in_degree[tgt] += 1
            
    queue = [(n_id, 0) for n_id, deg in in_degree.items() if deg == 0]
    if not queue and node_map:
        queue = [(list(node_map.keys())[0], 0)]
        
    node_levels = {}
    visited = set()
    while queue:
        u, lvl = queue.pop(0)
        if u in visited:
            continue
        visited.add(u)
        node_levels[u] = max(node_levels.get(u, 0), lvl)
        for v in adj[u]:
            queue.append((v, lvl + 1))
            
    max_lvl = max(node_levels.values()) if node_levels else 0
    for n_id in node_map:
        if n_id not in node_levels:
            node_levels[n_id] = max_lvl + 1
            
    from collections import defaultdict
    level_groups = defaultdict(list)
    for n_id, lvl in node_levels.items():
        level_groups[lvl].append(n_id)
        
    for lvl in sorted(level_groups.keys()):
        for idx, n_id in enumerate(level_groups[lvl]):
            node_map[n_id]['position'] = {
                "x": lvl * 300 + 100,
                "y": idx * 180 + 150
            }
            
    graph_data['nodes'] = nodes
    graph_data['edges'] = edges
    return graph_data

