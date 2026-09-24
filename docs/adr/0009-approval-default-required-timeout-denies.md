# ADR-0009: Human approval is required by default; timeouts deny, never auto-approve; a global kill switch exists

Status: Accepted

## Context

Even with a whitelisted action catalog and a deterministic policy engine,
someone has to decide the acceptable default posture for whether a human
must sign off before production changes, and what happens if that human
doesn't respond.

## Decision

- Default policy posture: every remediation requires human approval unless
  a specific, reviewed policy version explicitly allow-lists a low-tier
  action in a specific environment for unattended execution.
- Approval timeouts transition the incident to `ESCALATED`, never to an
  approved state. Silence is never consent.
- A global (and per-service) kill switch exists, checked by `policy-engine`
  before every decision, that forces `DENY` on all remediation regardless
  of any other rule. It is a `platform_admin`-only, fast, independent
  control — it does not depend on the rest of the pipeline being healthy
  to take effect.

## Alternatives considered

- **Auto-approve on timeout** (to avoid delaying remediation when no one's
  around): rejected — this is the exact shape of failure the system exists
  to prevent: an unattended, unreviewed production change. A missed
  approval should surface as an escalation, not silently proceed.
- **No kill switch, rely on scaling down services to stop remediation**:
  rejected — too slow and too blunt for an incident scenario (possibly
  the same incident the kill switch is meant to help contain), and it
  would take down `investigation-agent`'s read-only investigation
  capability along with remediation, which isn't necessary.

## Consequences

The system is conservative by default — it will escalate to a human more
often than a maximally "autonomous" design would, especially early on
before specific action/environment combinations earn unattended-execution
status through policy review. This is the intended tradeoff: autonomy is
something the system earns per action/environment via reviewed policy
changes, not a default assumption.
