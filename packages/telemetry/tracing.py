"""Clean integration point for OpenTelemetry -- not wired in yet.

Phase 1 requirement 10 explicitly asks for this to be a preparation point,
not a real integration: call sites use `start_span` now and get real spans
later without changing. When OpenTelemetry is introduced, replace the body
of `start_span` with
`tracer.start_as_current_span(name, attributes=attributes)` and this
module's public interface does not need to change.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager


@contextmanager
def start_span(name: str, **attributes: object) -> Iterator[None]:
    del name, attributes  # no-op until OpenTelemetry is wired in
    yield
