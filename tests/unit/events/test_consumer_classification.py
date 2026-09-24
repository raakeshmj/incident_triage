from __future__ import annotations

import pytest

from packages.events.consumer import classify_delivery


@pytest.mark.parametrize(
    "delivery_count,max_deliveries,expected",
    [
        (1, 5, "process"),
        (5, 5, "process"),  # boundary: the Nth attempt still processes
        (6, 5, "poison"),
        (100, 5, "poison"),
        (1, 1, "process"),
        (2, 1, "poison"),
    ],
)
def test_classify_delivery(delivery_count, max_deliveries, expected):
    assert classify_delivery(delivery_count, max_deliveries) == expected
