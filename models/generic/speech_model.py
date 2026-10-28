"""
Generic speech-to-text model: serves any Hub ID for speech-to-text that no
other model file claims, through Transformers' automatic-speech-recognition
pipeline (Whisper, Moonshine, Parakeet, Qwen3-ASR, wav2vec2, ...). Audio
longer than 30 seconds is transcribed in chunks. The IDs below are only what
/models/list suggests.
"""

from typing import Any, Dict

from models import BaseModel, ModelCard, register
from models.media import decode_audio, resample
from models.tasks import SpeechToTextInput, Transcript


@register(task="speech-to-text", fallback=True, ids=[
    ModelCard("UsefulSensors/moonshine-tiny", "Moonshine tiny (27M): English transcription at Whisper-tiny speed, more accurate",
              ["english", "fast"]),
    ModelCard("UsefulSensors/moonshine-base", "Moonshine base (61M): English transcription, as accurate as Whisper small",
              ["english", "fast"]),
    ModelCard("nvidia/parakeet-tdt-0.6b-v3",
              "NVIDIA Parakeet TDT 0.6B v3: 25 European languages, more accurate than Whisper large and much faster "
              "(CC-BY-4.0)", ["multilingual", "fast"]),
    ModelCard("Qwen/Qwen3-ASR-0.6B",
              "Qwen3-ASR 0.6B: 30 languages and Chinese dialects, Whisper large accuracy at a fraction of the size",
              ["multilingual"]),
    ModelCard("distil-whisper/distil-large-v3.5",
              "Distil-Whisper large v3.5: English, more accurate than Whisper large v3 turbo and faster",
              ["english"]),
    ModelCard("openai/whisper-large-v3-turbo", "Whisper large v3 turbo: most accurate, needs a GPU to be quick",
              ["multilingual"]),
])
class SpeechToTextModel(BaseModel):
    """Any model Transformers' speech recognition pipeline loads"""

    half_on_mps = True

    def load(self):
        from transformers import pipeline

        # The pipeline loads on the CPU and then moves the model: never
        # device_map on MPS (see base.py).
        self.pipeline = pipeline(
            "automatic-speech-recognition", model=self.model_id, dtype=self.dtype, device=self.device,
            trust_remote_code=False,
        )

    def predict(self, request: SpeechToTextInput) -> Transcript:
        samples, rate = decode_audio(request.audio)
        target = self.pipeline.feature_extractor.sampling_rate
        audio = {"raw": resample(samples, rate, target), "sampling_rate": target}

        generate: Dict[str, Any] = {}
        # Language, prompt and temperature are Whisper's; other models ignore them.
        if self.pipeline.type == "seq2seq_whisper":
            if request.language:
                generate["language"] = request.language
            if request.prompt:
                generate["prompt_ids"] = self.pipeline.tokenizer.get_prompt_ids(request.prompt, return_tensors="pt").to(
                    self.pipeline.device)
            if request.temperature > 0:
                generate.update(do_sample=True, temperature=request.temperature)
        options: Dict[str, Any] = {"chunk_length_s": 30}
        if generate:
            options["generate_kwargs"] = generate
        out = self.pipeline(audio, **options)
        return Transcript(text=out["text"].strip(), language=request.language)
