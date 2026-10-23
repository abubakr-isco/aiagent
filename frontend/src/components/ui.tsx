import { ReactNode, useEffect, useRef, useState } from "react";

import type { Check } from "../api";

export function Section({ title, aside, children }: { title: string; aside?: ReactNode; children: ReactNode }) {
  return (
    <section className="section">
      <header className="section-head">
        <h2>{title}</h2>
        {aside && <div className="section-aside">{aside}</div>}
      </header>
      {children}
    </section>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="empty">{children}</p>;
}

export function ErrorNote({ error }: { error: string | null }) {
  if (!error) return null;
  return <p className="error-note" role="alert">{error}</p>;
}

export function Side({ side }: { side: "BUY" | "SELL" }) {
  return <span className={`side side-${side.toLowerCase()}`}>{side === "BUY" ? "Buy" : "Sell"}</span>;
}

const STATUS_TONE: Record<string, string> = {
  EXECUTED: "good", FILLED: "good", PARTIALLY_FILLED: "warn", CLOSED: "quiet", ANALYSIS_ONLY: "quiet", BLOCKED: "quiet",
  AI_REJECTED: "warn", RISK_REJECTED: "warn", EXEC_REJECTED: "bad", REJECTED: "bad", FAILED: "bad", UNKNOWN: "bad",
  DUPLICATE: "quiet", PENDING_SUBMIT: "warn", SUBMITTED: "warn", DONE: "good", PENDING: "warn", RUNNING: "warn",
  PASSED: "good", NOT_VALIDATED: "warn",
};

export function Status({ value }: { value: string }) {
  const label = value.replace(/_/g, " ").toLowerCase();
  return <span className={`status status-${STATUS_TONE[value] ?? "quiet"}`}>{label.charAt(0).toUpperCase() + label.slice(1)}</span>;
}

export function Checks({ checks, title }: { checks: Check[]; title: string }) {
  if (!checks?.length) return null;
  return (
    <div className="checks">
      <h4>{title}</h4>
      <ul>
        {checks.map((c, i) => (
          <li key={`${c.name}-${i}`} className={c.passed ? "ok" : c.mandatory ? "fail" : "info"}>
            <span className="mark" aria-label={c.passed ? "passed" : "failed"}>{c.passed ? "✓" : c.mandatory ? "✕" : "·"}</span>
            <span className="check-name">{c.name.replace(/_/g, " ")}</span>
            <span className="check-detail">{c.detail}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

/** Modal confirmation. Optional typed phrase / password for dangerous actions. */
export function Confirm({
  title, body, phrase, password, confirmLabel, danger, onConfirm, onClose,
}: {
  title: string;
  body: ReactNode;
  phrase?: string;
  password?: boolean;
  confirmLabel: string;
  danger?: boolean;
  onConfirm: (values: { phrase: string; password: string }) => Promise<void>;
  onClose: () => void;
}) {
  const [typed, setTyped] = useState("");
  const [pw, setPw] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const dialog = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    dialog.current?.showModal();
  }, []);

  const ready = (!phrase || typed === phrase) && (!password || pw.length > 0) && !busy;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!ready) return;
    setBusy(true);
    setError(null);
    try {
      await onConfirm({ phrase: typed, password: pw });
      onClose();
    } catch (err) {
      setError((err as Error).message);
      setBusy(false);
    }
  }

  return (
    <dialog ref={dialog} className={`confirm ${danger ? "confirm-danger" : ""}`} onClose={onClose}>
      <form onSubmit={submit}>
        <h3>{title}</h3>
        <div className="confirm-body">{body}</div>
        {phrase && (
          <label className="field">
            <span>Type <strong>{phrase}</strong> to confirm</span>
            <input value={typed} onChange={(e) => setTyped(e.target.value)} autoFocus autoComplete="off" spellCheck={false} />
          </label>
        )}
        {password && (
          <label className="field">
            <span>Your password</span>
            <input type="password" value={pw} onChange={(e) => setPw(e.target.value)} autoComplete="current-password" />
          </label>
        )}
        <ErrorNote error={error} />
        <div className="confirm-actions">
          <button type="button" className="btn" onClick={onClose}>Cancel</button>
          <button type="submit" className={`btn ${danger ? "btn-danger" : "btn-primary"}`} disabled={!ready}>
            {busy ? "Sending…" : confirmLabel}
          </button>
        </div>
      </form>
    </dialog>
  );
}

export function Sparkline({ points, height = 140 }: { points: number[]; height?: number }) {
  if (points.length < 2) return <Empty>Not enough closed trades to draw an equity curve.</Empty>;
  const w = 600;
  const min = Math.min(...points);
  const max = Math.max(...points);
  const span = max - min || 1;
  const d = points
    .map((p, i) => `${i ? "L" : "M"}${((i / (points.length - 1)) * w).toFixed(1)},${(height - ((p - min) / span) * (height - 10) - 5).toFixed(1)}`)
    .join(" ");
  const up = points[points.length - 1] >= points[0];
  return (
    <svg className="sparkline" viewBox={`0 0 ${w} ${height}`} preserveAspectRatio="none" role="img" aria-label={`Equity from ${min.toFixed(0)} to ${max.toFixed(0)}`}>
      <path d={d} className={up ? "line-pos" : "line-neg"} fill="none" vectorEffect="non-scaling-stroke" />
    </svg>
  );
}
