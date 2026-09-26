import { Button, InlineNotification, Modal, SkeletonText } from "@carbon/react";
import { useCallback, useEffect, useState, type ReactNode } from "react";

import { ApiError, api } from "../api/client";
import type { EvidenceDetail } from "../api/types";
import { dateTime, label, shortId, tone } from "../api/format";

export function StatusTag({ value, prefix }: { value: string | null | undefined; prefix?: string }) {
  if (!value) return <span className="ii-muted">—</span>;
  return (
    <span className={`ii-tag ii-tone-${tone(value)}`} data-status={value}>
      <span className="ii-dot" aria-hidden="true" />
      {prefix ? label(`${prefix}_${value}`) : label(value)}
    </span>
  );
}

export function Panel({ title, meta, children, id }: { title: string; meta?: ReactNode; children: ReactNode; id?: string }) {
  return (
    <section className="ii-panel" aria-labelledby={id ? `${id}-title` : undefined} id={id}>
      <header>
        <h2 id={id ? `${id}-title` : undefined}>{title}</h2>
        {meta ? <div className="ii-panel-meta">{meta}</div> : null}
      </header>
      <div className="ii-panel-body">{children}</div>
    </section>
  );
}

// A horizontally scrollable region (wide tables on narrow screens) that
// keyboard users can focus and scroll.
export function ScrollRegion({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="ii-scroll" role="region" aria-label={`${label} (table)`} tabIndex={0}>
      {children}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="ii-empty">{children}</p>;
}

export function LoadError({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const message = error instanceof ApiError ? error.message : "Something went wrong.";
  return (
    <div className="ii-error" role="alert">
      <InlineNotification kind="error" lowContrast hideCloseButton title="Could not load" subtitle={message} />
      {onRetry ? (
        <Button kind="tertiary" size="sm" onClick={onRetry}>
          Retry
        </Button>
      ) : null}
    </div>
  );
}

export function Loading({ lines = 4 }: { lines?: number }) {
  return (
    <div aria-busy="true" aria-live="polite">
      <span className="cds--visually-hidden">Loading…</span>
      <SkeletonText paragraph lineCount={lines} />
    </div>
  );
}

export function useLoad<T>(load: () => Promise<T>, deps: unknown[], refreshMs?: number) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const run = useCallback(load, deps);
  const refresh = useCallback(async () => {
    try {
      setData(await run());
      setError(null);
    } catch (e) {
      setError(e);
    } finally {
      setLoading(false);
    }
  }, [run]);
  useEffect(() => {
    setLoading(true);
    void refresh();
    if (!refreshMs) return;
    const timer = window.setInterval(() => void refresh(), refreshMs);
    return () => window.clearInterval(timer);
  }, [refresh, refreshMs]);
  return { data, error, loading, refresh };
}

// --- evidence -----------------------------------------------------------------------

export function EvidenceLinks({ ids, onOpen }: { ids: string[]; onOpen: (id: string) => void }) {
  if (!ids.length) return <span className="ii-muted">none</span>;
  return (
    <span className="ii-evidence-links">
      {ids.map((id) => (
        <button key={id} type="button" className="ii-evidence-link" onClick={() => onOpen(id)} aria-label={`Open evidence ${id}`}>
          {shortId(id)}
        </button>
      ))}
    </span>
  );
}

export function EvidenceModal({ id, onClose }: { id: string | null; onClose: () => void }) {
  const [record, setRecord] = useState<EvidenceDetail | null>(null);
  const [error, setError] = useState<unknown>(null);
  useEffect(() => {
    if (!id) return;
    setRecord(null);
    setError(null);
    api.evidence(id).then(setRecord, setError);
  }, [id]);
  return (
    <Modal open={!!id} passiveModal modalHeading={id ? `Evidence ${shortId(id)}` : ""} modalLabel="Evidence record" onRequestClose={onClose} size="md">
      {/* initial focus on the content, not the close button: its tooltip
          would swallow the first Escape */}
      <div data-modal-primary-focus tabIndex={-1} className="ii-focus-target">
      {error ? <LoadError error={error} /> : null}
      {!record && !error ? <Loading /> : null}
      {record ? (
        <div className="ii-evidence">
          <p className="ii-evidence-summary">{record.summary}</p>
          <dl className="ii-kv">
            <dt>Type</dt>
            <dd>
              {record.evidence_type} · {record.source_system}
            </dd>
            <dt>Operation</dt>
            <dd className="ii-mono">{record.operation}</dd>
            <dt>Service</dt>
            <dd>{record.subject_service}</dd>
            <dt>Observed</dt>
            <dd className="ii-mono">{dateTime(record.observed_at)}</dd>
            <dt>Collected by</dt>
            <dd className="ii-mono">{record.requested_by}</dd>
            <dt>Content hash</dt>
            <dd className="ii-mono ii-break">{record.content_hash}</dd>
            <dt>Evidence id</dt>
            <dd className="ii-mono ii-break">{record.evidence_id}</dd>
          </dl>
          <h3 className="ii-subhead">Normalized payload</h3>
          <pre className="ii-pre">{JSON.stringify(record.normalized_payload, null, 2).slice(0, 6000)}</pre>
        </div>
      ) : null}
      </div>
    </Modal>
  );
}
