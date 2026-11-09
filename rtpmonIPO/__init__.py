"""Selective RTP-end streaming for Avaya IP Office SysMonitor."""

__version__ = "0.1.0"

from .client import IpoClient
from .errors import IpoConnectionError, IpoConnectionFailures
from .models import RtpSummary

__all__: list[str] = [
    "IpoClient",
    "IpoConnectionError",
    "IpoConnectionFailures",
    "RtpSummary",
    "__version__",
]
