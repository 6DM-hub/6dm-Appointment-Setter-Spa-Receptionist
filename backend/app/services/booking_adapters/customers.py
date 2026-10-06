"""Provider-neutral customer lookup results.

Adapters return these. The booking service stores a tenant-scoped link.
Nothing here talks to a specific provider or to the database.
"""
from dataclasses import dataclass
from enum import Enum


class CustomerLookupOutcome(str, Enum):
    UNSUPPORTED = "unsupported"
    SEARCH_FAILED = "search_failed"
    NO_MATCH = "no_match"
    MATCHED = "matched"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class ProviderCustomer:
    """A customer as the external provider already has them.

    `external_customer_id` is for our database and the booking API. It must
    not be read aloud to the caller.
    """

    external_customer_id: str
    display_name: str | None = None
    given_name: str | None = None
    family_name: str | None = None
    phone: str | None = None
    email: str | None = None


@dataclass(frozen=True)
class CustomerLookupResult:
    outcome: CustomerLookupOutcome
    customer: ProviderCustomer | None = None
    candidates: tuple[ProviderCustomer, ...] = ()
    detail: str | None = None


def caller_profile_name(
    *,
    caller_name: str | None,
    guest_name: str | None,
    customer_name: str | None,
) -> str | None:
    """Name that belongs to the caller, never the appointment guest.

    A self-booking that only has `customer_name` still counts. A guest name
    copied into `customer_name` does not.
    """
    caller = " ".join((caller_name or "").split())
    guest = " ".join((guest_name or "").split())
    customer = " ".join((customer_name or "").split())
    if caller:
        return caller
    if guest and (not customer or customer.casefold() == guest.casefold()):
        return None
    return customer or None


def unique_customer_by_name(
    candidates: tuple[ProviderCustomer, ...] | list[ProviderCustomer],
    spoken: str | None,
) -> ProviderCustomer | None:
    """Return one candidate only when the spoken name identifies exactly one.

    A single given name matches only when no other candidate shares it.
    Anything still tied stays unresolved.new name matches only when no other candidate shares it. Anything still tied stays unresolved.
    """
    spoken_cf = " ".join((spoken or "").split()).casefold()
    if not spoken_cf:
        return None
    exact = [
        candidate
        for candidate in candidates
        if " ".join((candidate.display_name or "").split()).casefold() == spoken_cf
    ]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        return None
    if len(spoken_cf.split()) != 1:
        return None
    given_hits: list[ProviderCustomer] = []
    for candidate in candidates:
        given = (candidate.given_name or "").strip().casefold()
        if not given and candidate.display_name:
            given = candidate.display_name.split()[0].casefold()
        if given == spoken_cf:
            given_hits.append(candidate)
    if len(given_hits) == 1:
        return given_hits[0]
    return None


def ambiguous_customer_message(
    candidates: tuple[ProviderCustomer, ...] | list[ProviderCustomer],
) -> str:
    """Instructions for the receptionist. Display names only, never provider ids."""
    labels: list[str] = []
    for candidate in candidates:
        label = " ".join((candidate.display_name or "").split()) or "an unnamed profile"
        labels.append(label)
    listed = "; ".join(labels) if labels else "unnamed profiles"
    return (
        "More than one client profile is associated with this phone number. "
        "Ask which name the appointment is under. Do not choose a profile and "
        "do not create a new one. Names on file: "
        f"{listed}."
    )
