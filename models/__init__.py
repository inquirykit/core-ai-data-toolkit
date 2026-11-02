"""
Model management: the registry, the LRU cache and the manager.

Model implementations live in the subfolders of families/ and are found by
``registry.discover()``; see models/README.md to add one. Models served by
OpenAI-compatible servers are in servers/.
"""

from .base import BaseModel, ModelInfo
from .cache import CapacityError, ModelCache
from .manager import ModelManager
from .registry import ModelCard, UnsupportedModel, register

__all__ = [
    "BaseModel",
    "CapacityError",
    "ModelCache",
    "ModelCard",
    "ModelInfo",
    "ModelManager",
    "UnsupportedModel",
    "register",
]
