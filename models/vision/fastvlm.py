"""
Apple FastVLM for vision-language tasks (text plus an optional image), from
Apple's own repos, which need their Hub code (trust_remote_code).

Converted checkpoints that Transformers supports natively (FastVlm, e.g.
KamilaMila/FastVLM-0.5B) need no Hub code and are served by the generic
ImageTextToTextModel instead.
"""

import io
from typing import Any, Dict, Optional, Tuple

from models import BaseModel, ModelCard, register
from models.base import generated_text
from models.tasks import GeneratedText, VisionLanguageInput, data_url_bytes

# Placeholder token id FastVLM's remote code expands into image features.
IMAGE_TOKEN_INDEX = -200


def _remote_code_allowed(model_id: str) -> bool:
    """Only allow-listed repos may run Hub code; any apple/FastVLM* ID is
    routed here, so a name match alone must not be enough."""
    try:
        from config import Config
    except Exception:
        return False
    return Config.TRUST_REMOTE_CODE and model_id in Config.TRUST_REMOTE_CODE_MODELS


@register(task="vision-language", ids=[
    ModelCard("apple/FastVLM-1.5B", "Apple FastVLM: quick image description and visual Q&A (needs TRUST_REMOTE_CODE)",
              ["chat", "image"]),
], match="apple/fastvlm*")
class FastVLMModel(BaseModel):
    """FastVLM vision-language model implementation"""

    default_max_length = 128
    half_on_mps = True

    @property
    def trust_remote_code(self) -> bool:
        return _remote_code_allowed(self.model_id)

    def load(self):
        if not self.trust_remote_code:
            raise ValueError(
                f"{self.model_id} needs trust_remote_code; add it to TRUST_REMOTE_CODE_MODELS to allow it"
            )

        from transformers import AutoModelForCausalLM, AutoTokenizer

        self.tokenizer = AutoTokenizer.from_pretrained(self.model_id, trust_remote_code=True, use_fast=False)

        self.model = self.load_pretrained(AutoModelForCausalLM, trust_remote_code=True)

    def _build_inputs(self, text_prompt: str, image):
        """Build input_ids/attention_mask/pixel tensors the way the model card does."""
        import torch

        tok = self.tokenizer
        model_device = next(self.model.parameters()).device

        if image is None:
            messages = [{"role": "user", "content": text_prompt}]
            rendered = tok.apply_chat_template(messages, add_generation_prompt=True, tokenize=False)
            enc = tok(rendered, return_tensors="pt", add_special_tokens=False)
            input_ids = enc.input_ids.to(model_device)
            return input_ids, torch.ones_like(input_ids), None

        messages = [{"role": "user", "content": f"<image>\n{text_prompt}"}]
        rendered = tok.apply_chat_template(messages, add_generation_prompt=True, tokenize=False)
        pre, post = rendered.split("<image>", 1)
        pre_ids = tok(pre, return_tensors="pt", add_special_tokens=False).input_ids
        post_ids = tok(post, return_tensors="pt", add_special_tokens=False).input_ids
        img_tok = torch.tensor([[IMAGE_TOKEN_INDEX]], dtype=pre_ids.dtype)
        input_ids = torch.cat([pre_ids, img_tok, post_ids], dim=1).to(model_device)
        attention_mask = torch.ones_like(input_ids)

        processor = self.model.get_vision_tower().image_processor
        pixel_values = processor(images=image, return_tensors="pt")["pixel_values"]
        pixel_values = pixel_values.to(model_device, dtype=self.model.dtype)
        return input_ids, attention_mask, pixel_values

    @staticmethod
    def _prompt_and_image(request: VisionLanguageInput) -> Tuple[str, Optional[Any]]:
        """FastVLM answers one prompt about at most one image: from a chat, the
        last user turn's text and the chat's first image."""
        from PIL import Image

        if not request.messages:
            return request.text, Image.open(request.image).convert("RGB") if request.image else None
        prompt, image = request.text, None
        for turn in request.messages:
            parts = [] if isinstance(turn.content, str) else turn.content
            if turn.role == "user":
                prompt = turn.content if isinstance(turn.content, str) else "\n".join(
                    p.text for p in parts if p.type == "text")
            if any(p.type == "input_audio" for p in parts):
                raise ValueError("FastVLM does not take audio")
            for part in parts:
                if part.type == "image_url" and image is None:
                    image = Image.open(io.BytesIO(data_url_bytes(part.image_url.url))).convert("RGB")
        return prompt, image

    def predict(self, request: VisionLanguageInput) -> GeneratedText:
        """Answer request.text (or the chat in request.messages), about its image if it has one"""
        import torch

        prompt, image = self._prompt_and_image(request)
        input_ids, attention_mask, pixel_values = self._build_inputs(prompt, image)
        generate_kwargs: Dict[str, Any] = {
            "attention_mask": attention_mask,
            "max_new_tokens": request.max_length or self.default_max_length,
            **request.sampling(),
        }
        if pixel_values is not None:
            generate_kwargs["images"] = pixel_values

        with torch.no_grad():
            output = self.model.generate(inputs=input_ids, **generate_kwargs)

        return generated_text(self.tokenizer, output[0], input_ids.shape[1])
