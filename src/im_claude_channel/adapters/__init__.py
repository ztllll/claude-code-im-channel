"""Per-platform adapters. One adapter owns one bot connection."""

from .base import Adapter, IncomingMessage

__all__ = ["Adapter", "IncomingMessage"]
