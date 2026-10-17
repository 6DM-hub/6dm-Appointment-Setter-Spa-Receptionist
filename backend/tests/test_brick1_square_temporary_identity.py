"""A similar list entry cannot identify this create request."""
import copy
from types import SimpleNamespace

import pytest

from app.services.booking_adapters.base import BookingProviderError
from app.services.square_booking_recovery import create_with_recovery
from tests.test_brick1_square_lost_create import request_payload


@pytest.mark.parametrize("replay_available", [True, False])
async def test_temporary_error_never_adopts_an_unrelated_list_match(replay_available):
    payload = request_payload()
    calls, sent = [], []
    matching = {**copy.deepcopy(payload["booking"]), "id": "unrelated", "status": "ACCEPTED"}
    own = {**copy.deepcopy(matching), "id": "this-request"}

    async def request(method, path, **kwargs):
        calls.append((method, path))
        if method == "POST":
            sent.append(copy.deepcopy(kwargs["json"]))
            if len(sent) == 1 or not replay_available:
                raise BookingProviderError("temporary lost reply", code="TEMPORARY", retryable=True)
            return {"booking": copy.deepcopy(own)}
        if path == "/v2/bookings":
            return {"bookings": [copy.deepcopy(matching)]}
        assert path == "/v2/bookings/this-request"
        return {"booking": copy.deepcopy(own)}

    delegate = SimpleNamespace(_request=request)
    if replay_available:
        result = await create_with_recovery(delegate, payload)
        assert result["booking"]["id"] == "this-request", "ADOPTED AN UNRELATED BOOKING"
    else:
        with pytest.raises(BookingProviderError) as caught:
            await create_with_recovery(delegate, payload)
        assert caught.value.code == "BOOKING_OUTCOME_UNKNOWN"
    assert sent == [payload, payload]
    assert ("GET", "/v2/bookings") not in calls
