# -*- coding: utf-8 -*-

DEFAULT_PROMPTS = {
    "rag_chat": {
        "name": "知识库对话/编排模板",
        "description": "RAG 全量知识库聊天和指令编排时使用的基础模板",
        "required_variables": ["prefix", "kb_catalog", "context", "chat_history", "question"],
        "template": """{prefix}
【系统知识目录】
你连接了 AnsFlow 的全量知识库系统，当前包含以下库：
{kb_catalog}

【特殊指令】
1. 资产编排：写剧本输出 `__ANSIBLE_DRAFT__: {{"name": "...", "content": "..."}}`。
2. 流水线编排：输出 `__PIPELINE_DRAFT__: {{"nodes": [...], "edges": [...]}}`。
   注意：
   - 每一个节点必须是以下系统组件列表中支持的标准执行节点之一：
     - `ansible`: 运维节点，用来执行 ansible 剧本或临时指令。其 data 字典中必须定义 `playbook`（Ansible 剧本内容字符串）或 `exec`（待执行命令，如 shell 指令）。例如：`{{"id": "node_check", "type": "ansible", "name": "诊断服务", "data": {{"playbook": "--- ...", "ansible_task_id": null}}}}`。
     - `git_clone`: 代码仓库拉取节点。
     - `docker_build` / `kaniko_build`: 镜像构建节点。
     - `k8s_deploy`: K8s 部署节点。
     - `host_deploy`: 主机部署节点。
     - `http_webhook`: Webhook 发送节点。
   - 严禁输出 `trigger`、`start`、`event`、`input` 等触发/事件节点类型（告警接收作为隐式触发源，不需要体现在流水线节点中）。
   - 严禁输出 `manual`、`approval` 等人工审批节点类型（系统有专用的外部审批拦截流程，流水线内部无需添加任何审批节点）。
   - 严禁输出任何名为 “接收告警”、“告警接收”、“人工审批”、“人工审批修复”、“人工确认” 或具有类似逻辑功能的节点。

参考内容：
{context}

对话历史：
{chat_history}

用户问题：{question}
你的回答："""
    },
    "log_diagnosis": {
        "name": "日志诊断模板",
        "description": "SRE 日志诊断任务模板，包含日志上下文和参考知识库",
        "required_variables": ["prefix", "kb_catalog", "target_type", "target_name", "error_summary", "log_content", "context"],
        "template": """{prefix}
作为专业 SRE，分析以下日志并给出诊断结论。
【系统知识目录】
{kb_catalog}
【执行上下文】
- 类型: {target_type}, 名称: {target_name}, 摘要: {error_summary}
【错误日志】
{log_content}
【参考知识库】
{context}
请给出：故障根因、修复建议（包含编排指令标记）、预防措施。"""
    },
    "alert_diagnosis": {
        "name": "告警诊断模板",
        "description": "SRE 告警深度诊断任务模板，包含参考知识库和告警详情",
        "required_variables": ["prefix", "context", "query"],
        "template": """{prefix}
你是一个资深的 SRE 专家。请针对以下告警信息进行深度诊断。
【参考知识库】
{context}
【告警详情】
{query}
请给出：
1. 故障根因分析
2. 修复建议（包括具体的命令或操作步骤）
3. 预防措施"""
    },
    "timepoint_diagnosis": {
        "name": "时间点诊断模板",
        "description": "SRE 时间点诊断中心模板，要求输出结构化报告和 Markdown 报告",
        "required_variables": ["prefix", "diagnosis_context"],
        "template": """{prefix}
你是资深 SRE。请基于以下时间点诊断上下文，分析系统或项目在该时间窗口的异常现象、可能根因、需要继续验证的证据、建议处置步骤。
请优先关联日志、指标、告警、流水线和任务记录。如果某类上下文缺失，请明确说明本次诊断的证据限制。

请先输出一段固定格式的结构化 JSON，格式为：
__STRUCTURED_REPORT__:{{
  "summary": "...",
  "impact_scope": ["..."],
  "evidence": [{{"ref": "LOG-1", "finding": "..."}}],
  "possible_causes": [{{"title": "...", "confidence": "low|medium|high", "evidence_refs": ["LOG-1"]}}],
  "recommended_actions": [{{"action": "...", "priority": "low|medium|high", "evidence_refs": ["LOG-1"]}}],
  "risks": ["..."],
  "next_checks": ["..."]
}}

所有 evidence_refs 尽量引用 diagnosis_context.evidence_index 中的 ref，例如 LOG-1、METRIC-1、ALERT-1。
结构化 JSON 后面再输出 Markdown 诊断报告。

【时间点诊断上下文】
{diagnosis_context}"""
    },
    "dag_generation": {
        "name": "DAG 流水线生成模板",
        "description": "根据用户输入文本生成 DAG 流水线 JSON 的提示词",
        "required_variables": ["prompt_text"],
        "template": "你是一个专业的流水线专家。生成 JSON 格式组织好的 DAG：{prompt_text}"
    },
    "dag_refine": {
        "name": "DAG 流水线细化模板",
        "description": "基于已有流水线 DAG 结构和用户指示进行编辑和微调的提示词",
        "required_variables": ["current_pipeline", "prompt_text"],
        "template": """你是一个资深的 AnsFlow SRE 流水线编排专家。
当前流水线的节点和边关系如下：
{current_pipeline}

用户提出了以下修改/细化要求：
{prompt_text}

请遵循以下约束调整流水线：
1. 每一个节点必须是以下系统组件列表中支持的标准执行节点之一：
   - `ansible`: 运维节点，用来执行 ansible 剧本或临时指令。其 data 字典中必须定义 `playbook`（Ansible 剧本内容字符串）或 `exec`（待执行命令，如 shell 指令）。例如：{{"id": "node_check", "type": "ansible", "name": "诊断服务", "data": {{"playbook": "--- ...", "ansible_task_id": null}}}}
   - `git_clone`: 代码仓库拉取节点。
   - `docker_build` / `kaniko_build`: 镜像构建节点。
   - `k8s_deploy`: K8s 部署节点。
   - `host_deploy`: 主机部署节点。
   - `http_webhook`: Webhook 发送节点。
2. 严禁输出 `trigger`、`start`、`event`、`input` 等触发/事件节点类型（告警接收作为隐式触发源，不需要体现在流水线节点中）。
3. 严禁输出 `manual`、`approval` 等人工审批节点类型（系统有专用的外部审批拦截流程，流水线内部无需添加任何审批节点）。
4. 严禁输出任何名为 “接收告警”、“告警接收”、“人工审批”、“人工审批修复”、“人工确认” 或具有类似逻辑功能的节点。
5. 请生成完整的、调整后的 JSON 格式的 DAG 流水线数据，结构为：{{"nodes": [...], "edges": [...]}}，不要包含任何多余解释，只返回该 JSON 代码块（可用 ```json 包裹）。"""
    },
    "pipeline_explain": {
        "name": "流水线解释模板",
        "description": "用于对 DAG 流水线的节点和边关系进行人类可读解释的提示词",
        "required_variables": ["pipeline"],
        "template": "解释以下流水线逻辑：{pipeline}"
    },
    "vision_ocr": {
        "name": "视觉 OCR 提取模板",
        "description": "视觉多模态大模型解析图片/PDF 页面时的默认 Prompt",
        "required_variables": [],
        "template": "Describe all text and tables in this image in detail. Output only the content."
    }
}
