"""Pure domain models for Incident Intelligence.

No FastAPI, no SQLAlchemy, no database or HTTP concerns here -- see
docs/architecture/03-domain-model.md. Pydantic is used only as a
validation/serialization tool, not as a web or persistence framework.
"""
