"""Hard bounds on every evidence query.

These are enforced server-side in the evidence service, independent of
anything a caller (including a future agent) asks for: a caller can ask for
less, never more. See docs/architecture/08-evidence-model.md, "Bounds".
"""

from __future__ import annotations

from datetime import timedelta

# Time windows
MAX_WINDOW = timedelta(hours=3)
MIN_WINDOW = timedelta(minutes=1)
DEFAULT_WINDOW = timedelta(minutes=30)
# Change sources (deployments, config, commits) are sparse, and "what
# changed in the day before this broke" is the question -- so their windows
# may be longer than telemetry windows, still bounded by the lookback below.
MAX_CHANGE_WINDOW = timedelta(hours=48)
# How far before an incident was opened a query may reach -- enough for a
# baseline comparison and "what changed just before", no further.
MAX_LOOKBACK_BEFORE_INCIDENT = timedelta(hours=24)

# Result sizes
MAX_SERIES = 10
MAX_POINTS_PER_SERIES = 60
MAX_LOG_LINES_FETCHED = 500
MAX_LOG_LINES_RETURNED = 20
MAX_LOG_LINE_CHARS = 2_000
MAX_LOG_MESSAGE_CHARS = 300
MAX_TRACES = 20
MAX_SPANS_IN_SUMMARY = 50
MAX_DEPLOYMENTS = 20
MAX_CONFIG_CHANGES = 20
MAX_COMMITS = 20
MAX_FILES_PER_COMMIT = 50
MAX_SIMILAR_INCIDENTS = 10
HISTORY_CANDIDATE_POOL = 200

# Backend calls
BACKEND_TIMEOUT_SECONDS = 10.0
