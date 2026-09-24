"""Event delivery mechanics: the outbox envelope shape and publisher
abstraction.

This is deliberately separate from packages/domain, which owns *what an
event means*; this package owns *how an event travels* once it leaves the
outbox. See docs/architecture/05-event-model.md.
"""
