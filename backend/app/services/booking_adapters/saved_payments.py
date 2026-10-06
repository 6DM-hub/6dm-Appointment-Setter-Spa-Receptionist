"""Provider-neutral saved-payment-method lookup results.

These say whether a usable method exists. They do not carry a card number,
security code, or any other value that could be charged or stored as a card.
"""
from dataclasses import dataclass
from enum import Enum


class SavedPaymentLookupOutcome(str, Enum):
    UNSUPPORTED = "unsupported"
    FAILED = "failed"
    NO_USABLE_METHOD = "no_usable_method"
    USABLE = "usable"


@dataclass(frozen=True)
class SavedPaymentLookup:
    outcome: SavedPaymentLookupOutcome
    # Counts only. Card brand, last4, and expiration stay inside the adapter.
    usable_count: int = 0
    examined_count: int = 0


class SaveCardOutcome(str, Enum):
    """Result of saving a card on file. This is not a charge."""

    SAVED = "saved"
    REJECTED = "rejected"
    AMBIGUOUS = "ambiguous"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class SaveCardResult:
    outcome: SaveCardOutcome
    error_code: str | None = None
