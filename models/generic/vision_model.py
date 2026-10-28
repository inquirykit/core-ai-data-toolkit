"""
Generic vision-language model: serves any Hub ID for vision-language that no
other model file claims, through Transformers' image-text-to-text classes
(Qwen3.5 / Qwen3-VL / Qwen2.5-VL, Gemma 4 / Gemma 3, LFM2.5-VL, SmolVLM,
MiniCPM-V 4.6, Idefics3, LLaVA, FastVLM's converted checkpoints, ...). The
IDs below are only what /models/list suggests; they also serve text-only
chats, so the natively multimodal chat models (Qwen3.5) are listed here.
"""

from typing import Any, Callable, Dict, List, Optional

from models import BaseModel, ModelCard, register
from models.base import generated_text, text_streaming
from models.tasks import GeneratedText, VisionLanguageInput


def _hf_content(content: Any) -> List[Dict[str, Any]]:
    """One turn's content as processor blocks. image_url parts pass as they are
    (the processor reads OpenAI's shape); input_audio becomes an audio block."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    blocks = []
    for part in content:
        if part["type"] == "input_audio":
            audio = part["input_audio"]
            blocks.append({"type": "audio", "url": f"data:audio/{audio['format']};base64,{audio['data']}"})
        else:
            blocks.append(part)
    return blocks


@register(task="vision-language", fallback=True, ids=[
    ModelCard("Qwen/Qwen3.5-0.8B",
              "Qwen3.5 0.8B: chat, images and video in 200 languages; the best small model of 2026, about 2 GB",
              ["chat", "image", "video", "multilingual", "thinking"]),
    ModelCard("Qwen/Qwen3.5-2B",
              "Qwen3.5 2B: stronger chat, documents, charts and video; about 4 GB in bf16",
              ["chat", "image", "video", "multilingual", "thinking"]),
    ModelCard("Qwen/Qwen3.5-4B",
              "Qwen3.5 4B: the most capable model here for documents, charts and reasoning; about 10 GB in bf16",
              ["chat", "image", "video", "multilingual", "thinking", "tools"]),
    ModelCard("LiquidAI/LFM2.5-VL-450M",
              "Liquid LFM2.5-VL 450M: quickest image Q&A, under 1 GB (LFM Open License: free below $10M revenue)",
              ["chat", "image", "fast"]),
    ModelCard("LiquidAI/LFM2.5-VL-1.6B",
              "Liquid LFM2.5-VL 1.6B: fast image description and Q&A, about 3 GB (LFM Open License)",
              ["chat", "image", "fast"]),
    ModelCard("openbmb/MiniCPM-V-4.6",
              "MiniCPM-V 4.6 (1.3B): reads text in images and documents; Qwen3.5 0.8B plus SigLIP2",
              ["chat", "image", "ocr", "multilingual"]),
    ModelCard("lightonai/LightOnOCR-2-1B",
              "LightOnOCR-2 1B: turns a page image into text, not a chat model; about 2 GB (Apache 2.0)",
              ["image", "ocr", "multilingual"]),
    ModelCard("HuggingFaceTB/SmolVLM2-256M-Video-Instruct",
              "Tiny image, video and text chat model, quick on CPU; drop-in successor to SmolVLM-256M",
              ["chat", "image", "video"]),
    ModelCard("Qwen/Qwen3-VL-2B-Instruct",
              "Qwen3-VL 2B: images, documents and charts; the Qwen3 generation, no thinking step",
              ["chat", "image", "multilingual"]),
])
class ImageTextToTextModel(BaseModel):
    """Any Transformers model AutoModelForImageTextToText loads"""

    default_max_length = 256
    half_on_mps = True

    def load(self):
        from transformers import AutoModelForImageTextToText, AutoProcessor

        # Generic models never run Hub code; see FastVLMModel for the allow-listed case.
        self.processor = AutoProcessor.from_pretrained(self.model_id, trust_remote_code=False)
        self.model = self.load_pretrained(AutoModelForImageTextToText)

    def _messages(self, request: VisionLanguageInput) -> List[Dict[str, Any]]:
        if request.messages:
            turns = [turn.model_dump(exclude_none=True) for turn in request.messages]
            if any(p["type"] == "input_audio" for t in turns if not isinstance(t["content"], str) for p in t["content"]):
                if getattr(self.processor, "feature_extractor", None) is None:
                    raise ValueError(f"{self.model_id} does not take audio")
            return [{"role": t["role"], "content": _hf_content(t["content"])} for t in turns]
        content: List[Dict[str, Any]] = [{"type": "text", "text": request.text}]
        if request.image:
            content.insert(0, {"type": "image", "path": request.image})
        return [{"role": "user", "content": content}]

    def predict(self, request: VisionLanguageInput, on_text: Optional[Callable[[str], Any]] = None) -> GeneratedText:
        import torch

        inputs = self.processor.apply_chat_template(
            self._messages(request),
            add_generation_prompt=True,
            tokenize=True,
            return_dict=True,
            return_tensors="pt",
        ).to(self.model.device)
        # Floating inputs (pixel values) in the model's dtype; ids stay integers.
        inputs = {k: v.to(self.model.dtype) if torch.is_floating_point(v) else v for k, v in inputs.items()}
        with torch.no_grad():
            output = self.model.generate(
                **inputs,
                max_new_tokens=request.max_length or self.default_max_length,
                **request.sampling(),
                **text_streaming(self.processor.tokenizer, on_text),
            )
        return generated_text(self.processor, output[0], inputs["input_ids"].shape[1])
