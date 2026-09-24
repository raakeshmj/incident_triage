"""incident-core: the sole writer of Alert and Incident state.

See docs/architecture/02-component-boundaries.md and
docs/architecture/03-domain-model.md. Everything under packages/incident/db
touches Postgres; packages/incident/service.py is the only public entry
point other components (apps/api's routers) are expected to call.
"""
