"""Controlled, read-only Git adapter over the simulator repository.

There is no "run git" operation. Two semantic reads -- recent commits for a
service, and code changes (commit metadata + changed files + diff summary)
for a service in a window or for one commit -- each executed as a fixed
argv (never a shell), with every path restricted to the service's
`repo_paths` from the service catalog and a validated commit SHA. Output is
`--numstat` summaries, not patches, and commit text is sanitized. Author
emails are never collected.
"""

from __future__ import annotations

import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from packages.evidence import limits
from packages.evidence.adapters._http import iso
from packages.evidence.errors import (
    BackendTimeoutError,
    BackendUnavailableError,
    InvalidQueryError,
    ScopeViolationError,
)
from packages.evidence.models import Observation
from packages.evidence.sanitize import clean_text
from packages.evidence.types import EvidenceType, SourceSystem

SHA = re.compile(r"^[0-9a-f]{7,40}$")
_RECORD = "\x1e"
_FIELD = "\x1f"
_FORMAT = f"--format={_RECORD}%H{_FIELD}%an{_FIELD}%aI{_FIELD}%cI{_FIELD}%s"
GIT_TIMEOUT_SECONDS = 10


class GitAdapter:
    def __init__(self, repo_path: str | Path) -> None:
        self._repo = Path(repo_path).resolve()

    def _git(self, *args: str) -> str:
        argv = ["git", "-C", str(self._repo), "-c", "core.quotepath=off", *args]
        try:
            completed = subprocess.run(
                argv, capture_output=True, text=True, timeout=GIT_TIMEOUT_SECONDS, check=False
            )
        except subprocess.TimeoutExpired as exc:
            raise BackendTimeoutError("git timed out") from exc
        except OSError as exc:
            raise BackendUnavailableError("git is not available") from exc
        if completed.returncode != 0:
            if "unknown revision" in completed.stderr or "bad object" in completed.stderr:
                raise InvalidQueryError("unknown commit")
            raise BackendUnavailableError(f"git exited with status {completed.returncode}")
        return completed.stdout

    def _log(self, *args: str, paths: tuple[str, ...]) -> list[dict[str, Any]]:
        if not paths:
            raise ScopeViolationError("service has no repository paths in the service catalog")
        output = self._git("log", _FORMAT, *args, "--", *paths)
        commits = []
        for block in output.split(_RECORD):
            if not block.strip():
                continue
            header, _, rest = block.partition("\n")
            sha, author, authored, committed, subject = header.split(_FIELD, 4)
            files = []
            for line in rest.strip().splitlines():
                parts = line.split("\t")
                if len(parts) != 3:
                    continue
                added, deleted, path = parts
                files.append(
                    {
                        "path": clean_text(path, 200),
                        "additions": int(added) if added.isdigit() else None,
                        "deletions": int(deleted) if deleted.isdigit() else None,
                    }
                )
            commits.append(
                {
                    "sha": sha,
                    "author": clean_text(author, 80),
                    "authored_at": authored,
                    "committed_at": committed,
                    "subject": clean_text(subject, 200),
                    "files": files,
                }
            )
        return commits

    def recent_commits(
        self,
        *,
        service: str,
        paths: tuple[str, ...],
        until: datetime,
        reference_time: datetime,
        limit: int,
    ) -> Observation:
        argv = (f"--until={iso(until)}", f"--max-count={limit}")
        commits = self._log(*argv, paths=paths)
        for commit in commits:
            delta = reference_time - datetime.fromisoformat(commit["committed_at"])
            commit["seconds_before_incident"] = int(delta.total_seconds())
            del commit["files"]
        nearest = min(commits, key=lambda c: abs(c["seconds_before_incident"]), default=None)
        normalized = {
            "service": service,
            "paths": list(paths),
            "until": iso(until),
            "incident_time": iso(reference_time),
            "commits": commits,
            "nearest_to_incident": nearest["sha"] if nearest else None,
        }
        return Observation(
            evidence_type=EvidenceType.GIT_CHANGE,
            source_system=SourceSystem.GIT,
            operation="recent_commits",
            subject_service=service,
            query_spec={
                "template": "recent_commits",
                "params": {"service": service, "limit": limit, "until": iso(until)},
                "argv": ["git", "log", _FORMAT, *argv, "--", *paths],
            },
            source_reference={
                "repository": self._repo.name,
                "commits": [c["sha"] for c in commits],
            },
            raw_response={"commits": commits},
            raw_truncated=len(commits) >= limit,
            normalized_payload=normalized,
            summary=(
                f"{len(commits)} most recent commit(s) touching {service}; nearest to the "
                f"incident: {nearest['sha'][:12]} ({nearest['seconds_before_incident']}s before)"
                if nearest
                else f"no commits touching {service} before {iso(until)}"
            ),
            result_count=len(commits),
            observed_at=datetime.fromisoformat(commits[0]["committed_at"]) if commits else until,
        )

    def code_changes(
        self,
        *,
        service: str,
        paths: tuple[str, ...],
        start: datetime,
        end: datetime,
        sha: str | None,
        limit: int,
    ) -> Observation:
        if sha is not None:
            if not SHA.match(sha):
                raise InvalidQueryError("sha must be 7-40 lowercase hex characters")
            argv: tuple[str, ...] = ("--numstat", "--max-count=1", sha)
        else:
            argv = (
                "--numstat",
                f"--since={iso(start)}",
                f"--until={iso(end)}",
                f"--max-count={limit}",
            )
        commits = self._log(*argv, paths=paths)
        files_truncated = False
        for commit in commits:
            if len(commit["files"]) > limits.MAX_FILES_PER_COMMIT:
                commit["files"] = commit["files"][: limits.MAX_FILES_PER_COMMIT]
                files_truncated = True
            commit["diff_summary"] = {
                "files_changed": len(commit["files"]),
                "additions": sum(f["additions"] or 0 for f in commit["files"]),
                "deletions": sum(f["deletions"] or 0 for f in commit["files"]),
            }
        normalized: dict[str, Any] = {
            "service": service,
            "paths": list(paths),
            "window": None if sha else {"start": iso(start), "end": iso(end)},
            "sha": sha,
            "commits": commits,
            "totals": {
                "commits": len(commits),
                "files_changed": sum(c["diff_summary"]["files_changed"] for c in commits),
                "additions": sum(c["diff_summary"]["additions"] for c in commits),
                "deletions": sum(c["diff_summary"]["deletions"] for c in commits),
            },
        }
        if sha is not None and not commits:
            summary = f"commit {sha} does not touch {service}'s paths"
        else:
            totals = normalized["totals"]
            summary = (
                f"{totals['commits']} commit(s) touching {service}: {totals['files_changed']} "
                f"file(s), +{totals['additions']}/-{totals['deletions']}"
            )
        return Observation(
            evidence_type=EvidenceType.GIT_CHANGE,
            source_system=SourceSystem.GIT,
            operation="code_changes",
            subject_service=service,
            query_spec={
                "template": "code_changes",
                "params": {"service": service, "sha": sha, "limit": limit},
                "argv": ["git", "log", _FORMAT, *argv, "--", *paths],
            },
            source_reference={
                "repository": self._repo.name,
                "commits": [c["sha"] for c in commits],
            },
            raw_response={"commits": commits},
            raw_truncated=files_truncated or (sha is None and len(commits) >= limit),
            normalized_payload=normalized,
            summary=summary,
            result_count=len(commits),
            observed_at=datetime.fromisoformat(commits[0]["committed_at"]) if commits else end,
            window_start=None if sha else start,
            window_end=None if sha else end,
        )
