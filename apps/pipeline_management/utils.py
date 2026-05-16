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
