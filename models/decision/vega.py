"""
vegaml implementation for typed decisions (https://github.com/NandhaKishorM/vegaml)

A frozen Qwen3.5 backbone reads the state once and a small trained engine
answers each question with calibrated probabilities. No text is generated.
"""

import io
from typing import Any, Dict

from models import BaseModel, ModelCard, register
from models.tasks import DecisionInput


@register(task="decision", ids=[
    ModelCard("vega-0.8b", "vegaml on Qwen3.5 0.8B: typed answers with probabilities about a text or image",
              ["image", "calibrated"]),
    ModelCard("vega-4b", "vegaml on Qwen3.5 4B: slower, more accurate typed answers", ["image", "calibrated"]),
], match=["vega-*", "*/vega-*"])
class VegaDecisionModel(BaseModel):
    """Answers choice, score and boolean questions about a text or image state.

    Other vega-* or owner/vega-* IDs are loaded as Hub repos or local paths.
    """

    # Toolkit model IDs -> the argument vegaml.load() takes.
    variants = {
        "vega-0.8b": None,  # vegaml's default checkpoint
        "vega-4b": "4b",
    }

    def load(self):
        try:
            import vegaml
        except ImportError:
            raise RuntimeError("vegaml is not installed: uv sync --extra vega  (or: pip install vegaml[vision])") from None

        source = self.variants.get(self.model_id, self.model_id)
        self.engine = vegaml.load(source, device=self.device, mode="engine")

    @staticmethod
    def _open_image(data: bytes):
        from PIL import Image

        try:
            image = Image.open(io.BytesIO(data))
            if image.format not in ("PNG", "JPEG", "WEBP"):
                raise ValueError(f"unsupported format {image.format}")
            return image.convert("RGB")
        except Exception as e:
            raise ValueError(f"Image could not be decoded: {e}") from e

    def predict(self, request: DecisionInput) -> Dict[str, Any]:
        """vegaml's answers ({"answers", "n_tokens"}) to the request's questions"""
        questions = {name: q.model_dump() for name, q in request.questions.items()}
        if request.image is not None:
            # The image path loads the backbone's vision tower on first use.
            return self.engine.decide_image(self._open_image(request.image), questions)
        return self.engine.decide(request.state, questions)
