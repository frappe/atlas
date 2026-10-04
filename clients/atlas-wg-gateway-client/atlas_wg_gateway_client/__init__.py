
""" A client library for accessing Atlas WireGuard gateway """
from .client import AuthenticatedClient, Client

__all__ = (
    "AuthenticatedClient",
    "Client",
)
