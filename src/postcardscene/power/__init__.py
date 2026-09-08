"""Three bounded power capabilities; selection and runtime ownership are separate."""

from ._types import Kind, PowerResult, Reason, State, Status
from .cec import CecBackend
from .ddc import DdcBackend
from .signal import SignalBackend

__all__ = [
    "CecBackend",
    "DdcBackend",
    "SignalBackend",
    "Kind",
    "PowerResult",
    "Reason",
    "State",
    "Status",
]
