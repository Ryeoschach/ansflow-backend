import yaml
import uuid
from typing import Any, Dict, List

class YAMLToGraphParser:
    """
    将 .ansflow-ci.yml 转换为 ReactFlow 兼容的 graph_data 格式。
    """

    def __init__(self, yaml_content: str):
        self.data = yaml.safe_load(yaml_content)
        self.nodes = []
        self.edges = []
        # 用于追踪 Job 名称到节点 ID 的映射
        self.job_to_node_id = {}

    def parse(self) -> Dict[str, Any]:
        """
        核心解析入口
        """
        if not self.data or 'jobs' not in self.data:
            return {"nodes": [], "edges": []}

        jobs = self.data.get('jobs', {})
        
        # 1. 创建节点
        for job_name, job_config in jobs.items():
            # 使用 job_name 作为 ID，确保确定性
            node_id = job_name
            self.job_to_node_id[job_name] = node_id
            
            node_type = job_config.get('type', 'default')
            params = job_config.get('params', {})
            
            # 构造 ReactFlow 节点数据
            node = {
                "id": node_id,
                "type": node_type,
                "data": {
                    "label": job_name,
                    **params
                },
                # 默认位置信息，防止前端渲染重叠（虽然主要由后端执行，但前端展示也需要）
                "position": {"x": 0, "y": 0} 
            }
            self.nodes.append(node)

        # 2. 创建边 (依赖关系)
        for job_name, job_config in jobs.items():
            target_id = self.job_to_node_id[job_name]
            needs = job_config.get('needs', [])
            
            # 支持单个字符串或列表
            if isinstance(needs, str):
                needs = [needs]
                
            for source_job in needs:
                if source_job in self.job_to_node_id:
                    source_id = self.job_to_node_id[source_job]
                    edge = {
                        "id": f"edge_{source_id}_{target_id}",
                        "source": source_id,
                        "target": target_id,
                        "animated": True
                    }
                    self.edges.append(edge)

        # 3. 自动布局算法 (简单的分层布局，确保前端展示不混乱)
        self._auto_layout()

        return {
            "nodes": self.nodes,
            "edges": self.edges
        }

    def _auto_layout(self):
        """
        简单的分层布局：根据依赖层级计算 X/Y 坐标
        """
        levels = {}
        # 计算每个节点的层级
        def get_level(job_name):
            if job_name in levels:
                return levels[job_name]
            
            needs = self.data['jobs'][job_name].get('needs', [])
            if not needs:
                levels[job_name] = 0
                return 0
            
            if isinstance(needs, str):
                needs = [needs]
            
            max_parent_level = max([get_level(n) for n in needs if n in self.data['jobs']] or [-1])
            levels[job_name] = max_parent_level + 1
            return levels[job_name]

        for job_name in self.data.get('jobs', {}):
            get_level(job_name)

        # 按层级分配坐标
        level_counts = {}
        for node in self.nodes:
            job_name = node['data']['label']
            level = levels.get(job_name, 0)
            
            x = level * 250
            y = level_counts.get(level, 0) * 100
            
            node['position'] = {"x": x, "y": y}
            level_counts[level] = level_counts.get(level, 0) + 1
