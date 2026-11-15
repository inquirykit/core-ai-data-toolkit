"""
Which model class serves which model ID and task.

Every model file registers its class with ``@register``. On first use,
``discover()`` imports every ``.py`` file in the subfolders of
models/families/ so those decorators run; nothing else has to be edited to
add a model.

Resolution for (model_id, task):

1. classes that list ``model_id`` in ``ids``;
2. otherwise classes whose ``match`` patterns match it;
3. otherwise ``fallback`` classes (those serve any ID nobody else claims).

Model server entries (MODEL_SERVERS_FILE, and those saved from /admin) are
registered after every model file (see models/servers/registry.py), so they win
over a model file claiming the same IDs; an edit registers them again.

Within the first group that applies, the class registered for ``task`` wins
(the one registered last, if several are). If that group has none, the
request is rejected rather than handed to a class built for something else.

Listed IDs can carry a ``ModelCard`` (a description and tags), which the
model browser in /admin shows (GET /models/catalog). Besides the IDs it
registers, a class can list IDs that come and go (``BaseModel.listed``: the
models jobs here trained).
"""

import fnmatch
import logging
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Tuple, Union

from core.discovery import import_files

logger = logging.getLogger(__name__)

FAMILIES_DIR = Path(__file__).resolve().parent / "families"


class UnsupportedModel(ValueError):
    """No registered class serves this model ID for this task (a client error)."""


@dataclass(frozen=True)
class ModelCard:
    """A listed model ID and what the model browser says about it.

    description: one line on what the model is good for.
    tags:        short labels to filter by, e.g. "chat", "multilingual".
    """
    id: str
    description: str = ""
    tags: Tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "tags", _as_tuple(self.tags))

    def merged(self, later: "ModelCard") -> "ModelCard":
        """``later`` (a later registration of the same ID), keeping what it leaves out from this one."""
        return replace(later, description=later.description or self.description, tags=later.tags or self.tags)


@dataclass(frozen=True)
class Entry:
    cls: type
    task: str
    ids: Tuple[str, ...]
    match: Tuple[str, ...]
    fallback: bool
    cards: Tuple[ModelCard, ...] = ()

    def matches(self, model_id: str) -> bool:
        lowered = model_id.lower()
        return any(fnmatch.fnmatchcase(lowered, pattern.lower()) for pattern in self.match)


_entries: List[Entry] = []
_discovered = False


def _as_tuple(value: Union[str, Iterable[str]]) -> Tuple[str, ...]:
    return (value,) if isinstance(value, str) else tuple(value)


def register(
    *,
    task: str,
    ids: Iterable[Union[str, ModelCard]] = (),
    match: Union[str, Iterable[str]] = (),
    fallback: bool = False,
):
    """Class decorator that makes a model class servable.

    task:     the task this class serves. Stack the decorator for several.
    ids:      model IDs it serves, listed by /models/list and /v1/models:
              plain strings, or ModelCards to describe them in the model browser.
    match:    case-insensitive glob patterns for other IDs it serves,
              e.g. "*fastvlm*" or ["vega-*", "*/vega-*"].
    fallback: serve any ID for this task that no other class claims.
    """
    cards = tuple(i if isinstance(i, ModelCard) else ModelCard(i) for i in ids)

    def decorate(cls):
        _entries.append(Entry(cls, task, tuple(c.id for c in cards), _as_tuple(match), fallback, cards))
        if "tasks" not in cls.__dict__:
            cls.tasks = ()
        cls.tasks = (*cls.tasks, task)
        return cls

    return decorate


def unregister(cls: type) -> None:
    """Drop every registration of ``cls`` (model servers do, to register their entries again after an edit)."""
    _entries[:] = [e for e in _entries if e.cls is not cls]
    cls.tasks = ()


@contextmanager
def restored() -> Iterator[None]:
    """When the block ends, the registrations, and the tasks of the classes registered now, are as they were when it
    began, whatever was registered, swapped or dropped inside it: tests serve their own classes this way."""
    discover()
    saved = list(_entries)
    tasks = {e.cls: e.cls.__dict__.get("tasks") for e in saved}
    try:
        yield
    finally:
        _entries[:] = saved
        for cls, was in tasks.items():
            if was is None:
                cls.__dict__.get("tasks") is not None and delattr(cls, "tasks")
            else:
                cls.tasks = was


def swap(old: type, new: type) -> None:
    """Serve ``new`` wherever ``old`` is registered, with the same IDs, patterns and place (inside ``restored()``)."""
    discover()
    _entries[:] = [replace(e, cls=new) if e.cls is old else e for e in _entries]


def classes() -> List[type]:
    """Every registered class, once each, in the order they were first registered."""
    discover()
    return list(dict.fromkeys(e.cls for e in _entries))


def discover() -> None:
    """Import every model file once (discovery.import_files), so its @register runs."""
    global _discovered
    if _discovered:
        return
    _discovered = True
    import_files(FAMILIES_DIR, f"{__package__}.families", "model")
    # Last, so a model listed there is served by its server even if a model file also claims it.
    from .servers import load_configured

    load_configured()
    logger.info("Registered %d model entries", len(_entries))
    # Every model file is in, so each variant's task can be looked for; a wrong one stands alone, like a file that
    # fails to import, instead of taking the service down.
    from . import contracts

    for problem in contracts.verify():
        logger.error("Task variant: %s; it stands alone", problem)


def _candidates(model_id: str) -> Tuple[List[Entry], bool]:
    """The entries that claim ``model_id``, and whether the claim is specific."""
    discover()
    exact = [e for e in _entries if model_id in e.ids]
    if exact:
        return exact, True
    matched = [e for e in _entries if not e.fallback and e.matches(model_id)]
    if matched:
        return matched, True
    return [e for e in _entries if e.fallback], False


def resolve(model_id: str, task: str) -> type:
    """The class that serves ``model_id`` for ``task``, or UnsupportedModel."""
    return entry_for(model_id, task).cls


def entry_for(model_id: str, task: str) -> Entry:
    """The registration that serves ``model_id`` for ``task``, or UnsupportedModel."""
    candidates, specific = _candidates(model_id)
    for entry in reversed(candidates):
        if entry.task == task:
            return entry
    if specific:
        supported = ", ".join(sorted({e.task for e in candidates}))
        raise UnsupportedModel(f"{model_id} serves {supported}, not {task}")
    raise UnsupportedModel(f"Unknown task {task!r}. Known tasks: {', '.join(known_tasks())}")


def claimed_tasks(model_id: str) -> List[str]:
    """Tasks some class claims ``model_id`` for by ID or pattern (not by fallback). When a class that claims it by
    pattern also lists it (``BaseModel.listed``), only the tasks it lists it for: a predictor a job trained serves the
    one task it was trained for, whatever else its pattern covers."""
    candidates, specific = _candidates(model_id)
    if not specific:
        return []
    tasks = list(dict.fromkeys(e.task for e in candidates))
    own = [t for t in tasks if any(card.id == model_id for e in candidates if e.task == t for card in e.cls.listed(t))]
    return own or tasks


def operator_chose(model_id: str) -> bool:
    """Whether a class that claims ``model_id`` by ID or pattern says the operator chose it
    (``BaseModel.operator_chose``): clients may then use it whatever ALLOWED_MODELS says."""
    candidates, specific = _candidates(model_id)
    return specific and any(cls.operator_chose(model_id) for cls in dict.fromkeys(e.cls for e in candidates))


def prompts(model_id: str) -> Dict[str, str]:
    """The prompts the class serving ``model_id`` for embedding says it wants (``BaseModel.prompts``). It may fetch
    a file from the Hub, so it blocks."""
    return resolve(model_id, "embedding").prompts(model_id)


def known_tasks() -> List[str]:
    discover()
    return sorted({e.task for e in _entries})


def catalog() -> Dict[str, List[str]]:
    """task -> the model IDs registered for it (every known task is present)."""
    return {task: list(listed) for task, listed in cards().items()}


def cards() -> Dict[str, Dict[str, ModelCard]]:
    """task -> {model ID -> its card}, in the order the IDs were first registered, each class's listed ones
    (``BaseModel.listed``) after those it registers.

    When an ID is registered again (a model server taking over a model
    file's ID), the later card wins but keeps what it leaves out. Listing
    may read files (the jobs' folders), so it blocks.
    """
    discover()
    out: Dict[str, Dict[str, ModelCard]] = {task: {} for task in known_tasks()}
    for entry in _entries:
        listed = out[entry.task]
        for card in (*entry.cards, *entry.cls.listed(entry.task)):
            earlier = listed.get(card.id)
            listed[card.id] = card if earlier is None else earlier.merged(card)
    return out


def patterns(task: str) -> List[str]:
    """The glob patterns of the classes serving ``task``, for IDs nobody lists."""
    discover()
    return list(dict.fromkeys(p for e in _entries if e.task == task for p in e.match))


def accepts_any_id(task: str) -> bool:
    """Whether a fallback class serves any ID for ``task`` (any Hub ID, say)."""
    discover()
    return any(e.fallback for e in _entries if e.task == task)


def describe() -> List[Dict[str, Any]]:
    """Every registration, for introspection endpoints."""
    discover()
    return [
        {
            "class": f"{e.cls.__module__}.{e.cls.__qualname__}",
            "task": e.task,
            "ids": list(e.ids),
            "match": list(e.match),
            "fallback": e.fallback,
        }
        for e in _entries
    ]
