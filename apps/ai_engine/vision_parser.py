import base64
import requests
import logging
from typing import Optional
from .models import AIConfig, AIModel

logger = logging.getLogger(__name__)

class VisionParser:
    """
    通用视觉解析器。
    适配 LM Studio (OpenAI 兼容), Ollama, 以及云端 API。
    """
    def __init__(self, model_id: Optional[int] = None):
        self.config = self._get_vision_config(model_id)

    def _get_vision_config(self, model_id: Optional[int]):
        model = None
        if model_id:
            model = AIModel.objects.filter(id=model_id).first()
        
        if not model:
            global_config = AIConfig.objects.filter(name="default").first()
            if global_config:
                # 优先使用专门的视觉模型配置
                model = global_config.default_vision or global_config.default_llm

        if model:
            # 自动补全 /v1 后缀，LM Studio/OpenAI 规范
            base_url = model.provider.base_url.rstrip('/')
            if "localhost:1234" in base_url and not base_url.endswith('/v1'):
                base_url = f"{base_url}/v1"

            return {
                "name": model.name,
                "provider_type": model.provider.provider_type,
                "base_url": base_url,
                "api_key": model.provider.get_decrypted_key() or "not-needed"
            }
        
        # 环境变量兜底 (适配您的本地 LM Studio 测试环境)
        import os
        return {
            "name": os.environ.get("VISION_MODEL_NAME", "loaded-model"), # 请确保这里与 LM Studio 中显示的名称一致
            "provider_type": "openai", 
            "base_url": "http://localhost:1234/v1",
            "api_key": "not-needed"
        }

    def parse_image(self, image_bytes: bytes, custom_prompt: Optional[str] = None) -> str:
        base64_image = base64.b64encode(image_bytes).decode('utf-8')
        ptype = self.config["provider_type"]

        logger.info(f"[Vision] Requesting OCR via {ptype} at {self.config['base_url']}")
        
        # 更加严谨的判定：只有在真正为 None 或 全空格时才使用默认值
        prompt = custom_prompt if (custom_prompt and custom_prompt.strip()) else "Describe all text and tables in this image in detail. Output only the content."
        
        logger.info(f"[Vision] Using prompt: {prompt[:50]}...")
        
        # LM Studio 必须走 OpenAI 兼容逻辑
        if ptype in ["openai", "lmstudio", "other"] or "1234" in self.config['base_url']:
            return self._parse_via_openai_compatible(base64_image, prompt)
        elif ptype == "ollama":
            return self._parse_via_ollama(base64_image, prompt)
        return ""

    def _parse_via_openai_compatible(self, base64_image: str, prompt: str) -> str:
        url = f"{self.config['base_url']}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config['api_key']}"
        }
        # 注意：LM Studio 可能需要特定模型名称，或者接收任何值（取决于设置）
        payload = {
            "model": self.config["name"],
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
                    ]
                }
            ],
            "temperature": 0, # OCR 建议设为 0
            "stream": False
        }
        try:
            logger.info(f"[Vision] Sending payload to LM Studio, model: {self.config['name']}")
            response = requests.post(url, json=payload, headers=headers, timeout=120)
            if response.status_code != 200:
                logger.error(f"[Vision] Error response from server: {response.text}")
            response.raise_for_status()
            return response.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:
            logger.error(f"[Vision] OpenAI-compatible OCR failed: {e}")
            return ""

    def _parse_via_ollama(self, base64_image: str, prompt: str) -> str:
        # 保持对 Ollama 的兼容
        url = f"{self.config['base_url']}/api/generate"
        payload = {
            "model": self.config["name"],
            "prompt": prompt,
            "stream": False,
            "images": [base64_image]
        }
        try:
            response = requests.post(url, json=payload, timeout=120)
            response.raise_for_status()
            return response.json().get("response", "").strip()
        except Exception as e:
            logger.error(f"[Vision] Ollama OCR failed: {e}")
            return ""
