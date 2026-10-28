"""
Generic Hugging Face model: serves any Hub ID for these tasks that no other
model file claims. The IDs below are only what /models/list suggests.
"""

import logging
from typing import Any, Callable, Dict, List, Optional

from models import BaseModel, ModelCard, register, toolcalls
from models.base import answer_questions, fit_max_length, length_batches, text_streaming, with_stop_strings
from models.tasks import GeneratedText, TextClassificationOutput, TextGenerationInput, TokenLogprobs

logger = logging.getLogger(__name__)

AUTO_CLASSES = {
    "text-generation": "AutoModelForCausalLM",
    "text-classification": "AutoModelForSequenceClassification",
    "question-answering": "AutoModelForQuestionAnswering",
}


@register(task="text-generation", fallback=True, ids=[
    # Instruction-tuned with a chat template: the /playground default.
    ModelCard("Qwen/Qwen2.5-0.5B-Instruct", "Small instruction-tuned chat model, 29 languages", ["chat", "multilingual"]),
    # Qwen3.5 (natively multimodal) is listed under vision-language; the Chat tab shows both.
    ModelCard("LiquidAI/LFM2.5-1.2B-Instruct",
              "Liquid LFM2.5 1.2B: fastest chat per parameter on CPU, follows instructions well "
              "(LFM Open License: free below $10M revenue)", ["chat", "multilingual", "fast"]),
    ModelCard("LiquidAI/LFM2.5-230M", "Liquid LFM2.5 230M: tiny chat model, 10 languages (LFM Open License)",
              ["chat", "multilingual", "fast"]),
    ModelCard("ibm-granite/granite-4.1-3b",
              "IBM Granite 4.1 3B: direct answers with no thinking step; tools and code; about 6 GB in bf16",
              ["chat", "multilingual", "tools"]),
    ModelCard("HuggingFaceTB/SmolLM2-360M-Instruct", "Tiny English chat model, quick on CPU", ["chat", "english"]),
])
@register(task="text-classification", fallback=True, ids=[
    ModelCard("AnkitAI/Sensible-ModernBERT-Sentiment-Analysis",
              "ModernBERT English sentiment: negative or positive; texts up to 8K tokens", ["sentiment", "english"]),
    ModelCard("clapAI/mmBERT-small-multilingual-sentiment",
              "mmBERT small: negative, neutral or positive in 16+ languages; texts up to 8K tokens",
              ["sentiment", "multilingual"]),
    ModelCard("cardiffnlp/twitter-roberta-base-sentiment-latest",
              "Tweet sentiment: negative, neutral or positive", ["sentiment", "english"]),
    ModelCard("ProsusAI/finbert", "Financial news sentiment: positive, negative or neutral", ["sentiment", "finance"]),
])
@register(task="question-answering", fallback=True, ids=[
    ModelCard("deepset/tinyroberta-squad2",
              "Distilled RoBERTa answer spans (SQuAD 2.0): about twice as fast as roberta-base, nearly as accurate",
              ["extractive", "english", "fast"]),
    ModelCard("deepset/deberta-v3-base-squad2",
              "DeBERTa-v3 answer spans (SQuAD 2.0): the most accurate base-sized model", ["extractive", "english"]),
    ModelCard("Praise2112/ModernBERT-base-squad2-v0.2",
              "ModernBERT answer spans (SQuAD 2.0): passages up to 8K tokens, Apache-2.0", ["extractive", "english"]),
    ModelCard("deepset/deberta-v3-large-squad2",
              "DeBERTa-v3 large answer spans (SQuAD 2.0): most accurate, slowest", ["extractive", "english"]),
])
class TransformersModel(BaseModel):
    """Hugging Face Transformers model implementation"""

    @property
    def half_on_mps(self) -> bool:
        return self.task == "text-generation"

    def load(self):
        import transformers
        from transformers import AutoTokenizer

        model_class = getattr(transformers, AUTO_CLASSES[self.task])
        # Generic models never run Hub code; see FastVLMModel for the allow-listed case.
        self.tokenizer = AutoTokenizer.from_pretrained(self.model_id, trust_remote_code=False)
        self.model = self.load_pretrained(model_class)
        fit_max_length(self.tokenizer, self.model)

    default_max_length = 100
    # predict_batch(): texts (or question and context pairs) per forward pass,
    # and characters per pass once each is padded to the pass's longest.
    batch_size = 32
    batch_chars = 64_000

    def predict(self, request: Any, on_text: Optional[Callable[[str], Any]] = None) -> Any:
        import torch

        with torch.no_grad():
            if self.task == "text-generation":
                return self._generate(request, on_text)
            return self._score([request])[0]

    @property
    def batches(self) -> bool:
        # Generation runs one prompt at a time whatever arrives together.
        return self.task != "text-generation"

    def predict_batch(self, requests: List[Any]) -> List[Any]:
        """Classify texts, or answer questions, several to a forward pass; generate replies one by one.

        Similar lengths share a pass (see length_batches), so little of it is
        padding, and the results are put back in request order.
        """
        import torch

        if self.task == "text-generation":
            return [self.predict(request) for request in requests]
        # Texts of different lengths share a pass only padded, which needs a padding token.
        can_pad = self.tokenizer.pad_token is not None and getattr(self.model.config, "pad_token_id", None) is not None
        lengths = [sum(len(getattr(request, field, "")) for field in ("text", "question", "context")) for request in requests]
        results: List[Any] = [None] * len(requests)
        with torch.no_grad():
            for batch in length_batches(lengths, self.batch_size if can_pad else 1, self.batch_chars):
                for i, result in zip(batch, self._score([requests[i] for i in batch])):
                    results[i] = result
        return results

    def _score(self, requests: List[Any]) -> List[Any]:
        """Classify or answer ``requests`` in one forward pass."""
        if self.task == "question-answering":
            pairs = [(request.question, request.context) for request in requests]
            return answer_questions(self.model, self.tokenizer, pairs, max_length=self.tokenizer.model_max_length)
        return self._classify([request.text for request in requests])

    def _generate(self, request: TextGenerationInput, on_text: Optional[Callable[[str], Any]]) -> GeneratedText:
        """Continue the prompt (or reply to the chat) with generate(), so each token's log probability is known.

        /v1/chat/completions sends the chat as well as a plain transcript
        (text); chat-tuned models answer far better in the format they were
        trained on, so a chat goes through the tokenizer's template. Token IDs
        (``tokens``) go in as they are. With ``echo`` the prompt's tokens are
        scored too (one forward pass), which is how lm-evaluation-harness
        computes a choice's likelihood.
        """
        import torch

        if request.tokens is not None:
            ids = list(request.tokens)
        else:
            chat = self._render_chat(request) if request.messages else None
            ids = self.tokenizer(chat if chat is not None else request.text, add_special_tokens=chat is None)["input_ids"]
        max_new = self.default_max_length if request.max_length is None else request.max_length
        # Keep the end of an over-long prompt, leaving room for the new tokens.
        room = max(1, self.tokenizer.model_max_length - max_new)
        ids = ids[-room:]
        device = self.model.device
        input_ids = torch.tensor([ids], device=device)
        attention = torch.ones_like(input_ids)
        top_k = request.logprobs or 0
        want_logprobs = request.logprobs is not None

        new_ids: List[int] = []
        new_logprobs: List[float] = []
        new_top: List[Dict[str, float]] = []
        if max_new > 0:
            streaming = with_stop_strings(text_streaming(self.tokenizer, on_text), self.tokenizer, request.stop)
            out = self.model.generate(
                input_ids,
                attention_mask=attention,
                max_new_tokens=max_new,
                pad_token_id=self.tokenizer.pad_token_id or self.tokenizer.eos_token_id,
                return_dict_in_generate=True,
                output_logits=want_logprobs,
                **request.sampling(),
                **streaming,
            )
            new_ids = out.sequences[0][len(ids):].tolist()
            if want_logprobs:
                for step, token in zip(out.logits, new_ids):
                    logprob, top = self._top(torch.log_softmax(step[0].float(), dim=-1), token, top_k)
                    new_logprobs.append(logprob)
                    new_top.append(top)
        text = self.tokenizer.decode(new_ids, skip_special_tokens=True)
        result = GeneratedText(text=text, prompt_tokens=len(ids), completion_tokens=len(new_ids))
        if not want_logprobs:
            return result

        tokens: List[str] = []
        token_logprobs: List[Optional[float]] = []
        top_logprobs: List[Optional[Dict[str, float]]] = []
        if request.echo:
            with torch.no_grad():
                logits = self.model(input_ids=input_ids, attention_mask=attention).logits[0].float()
            tokens.append(self.tokenizer.decode([ids[0]]))
            token_logprobs.append(None)
            top_logprobs.append(None)
            for position in range(1, len(ids)):
                logprob, top = self._top(torch.log_softmax(logits[position - 1], dim=-1), ids[position], top_k)
                tokens.append(self.tokenizer.decode([ids[position]]))
                token_logprobs.append(logprob)
                top_logprobs.append(top)
        tokens += [self.tokenizer.decode([t]) for t in new_ids]
        token_logprobs += new_logprobs
        top_logprobs += new_top
        offsets, at = [], 0
        for token in tokens:
            offsets.append(at)
            at += len(token)
        result.logprobs = TokenLogprobs(
            tokens=tokens, token_logprobs=token_logprobs, text_offset=offsets,
            top_logprobs=top_logprobs if top_k else None,
        )
        return result

    def _top(self, logprobs: Any, token: int, top_k: int) -> "tuple[float, Dict[str, float]]":
        """The token's log probability and the ``top_k`` most likely tokens at that position."""
        top: Dict[str, float] = {}
        if top_k:
            values, indices = logprobs.topk(top_k)
            for value, index in zip(values.tolist(), indices.tolist()):
                top[self.tokenizer.decode([index])] = value
        return float(logprobs[token]), top

    def _classify(self, texts: List[str]) -> List[TextClassificationOutput]:
        """Every label's score for each text, best first, as the text-classification pipeline gives them: a sigmoid
        for multi-label models and single-logit ones, a softmax otherwise, the raw value for regression."""
        inputs = self.tokenizer(texts, padding=len(texts) > 1, truncation=True, return_tensors="pt").to(self.model.device)
        logits = self.model(**inputs).logits.float()
        config = self.model.config
        if config.problem_type == "regression":
            scores = logits
        elif config.problem_type == "multi_label_classification" or logits.shape[-1] == 1:
            scores = logits.sigmoid()
        else:
            scores = logits.softmax(-1)
        results = []
        for row in scores.tolist():
            ranked = sorted(((config.id2label[i], float(v)) for i, v in enumerate(row)), key=lambda r: -r[1])
            results.append(TextClassificationOutput(label=ranked[0][0], confidence=ranked[0][1], scores=dict(ranked)))
        return results

    def _render_chat(self, request: TextGenerationInput) -> Optional[str]:
        """The chat in the tokenizer's chat template, or None if it has none or rejects the chat.

        Tools go to a template that takes them (it renders them and teaches
        the model's own call format); otherwise, like a JSON schema, they
        become an instruction in the system turn (see models/toolcalls.py).
        """
        template = getattr(self.tokenizer, "chat_template", None)
        if not template:
            return None
        messages = [turn.model_dump(exclude_none=True) for turn in request.messages]
        tools = request.tools if request.tools and request.tool_choice != "none" else None
        native = tools is not None and "tools" in template
        guide = toolcalls.instructions(tools, request.tool_choice, request.json_schema, listed=not native)
        messages = toolcalls.template_messages(toolcalls.with_instructions(messages, guide))
        extra = {"tools": tools} if native else {}
        # Some templates refuse a system turn; retry with it folded into the first user turn.
        for chat in (messages, fold_system(messages)):
            try:
                return self.tokenizer.apply_chat_template(
                    chat,
                    add_generation_prompt=True,
                    tokenize=False,
                    # Templates with a thinking mode (Qwen3, SmolLM3) would
                    # spend the token budget on it; others ignore the flag.
                    enable_thinking=False,
                    **extra,
                )
            except Exception as e:  # templates raise their own errors for chats they refuse
                error = e
        logger.info("%s: chat template rejected the chat (%s); using a plain transcript", self.model_id, error)
        return None


def fold_system(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Move system turns into the first user turn, for templates without a system role."""
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    rest = [dict(m) for m in messages if m["role"] != "system"]
    if system:
        first_user = next((m for m in rest if m["role"] == "user"), None)
        if first_user is None:
            rest.insert(0, {"role": "user", "content": system})
        else:
            first_user["content"] = f"{system}\n\n{first_user['content']}"
    return rest
