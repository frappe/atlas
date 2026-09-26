"""Standalone WireGuard gateway daemon. Central calls it through the HTTP proxy."""

from .auth import Authorization, authentication
from .main import app

__all__ = ["Authorization", "app", "authentication"]
