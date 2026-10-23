import { Fragment, useState } from "react";

import type { Paged, SignalRow } from "../api";
import { Checks, Empty, ErrorNote, Section, Side, Status } from "../components/ui";
import { money, pct, price, time } from "../format";
import { usePoll } from "../hooks";

const STATUSES = ["", "EXECUTED", "RISK_REJECTED", "AI_REJECTED", "BLOCKED", "ANALYSIS_ONLY", "EXEC_REJECTED", "UNKNOWN"];

function Detail({ s }: { s: SignalRow }) {
  return (
    <div className="detail">
      <p>{s.note}</p>
      <div className="detail-grid">
        <Checks title="Strategy rules" checks={s.checks} />
        <div className="checks">
          <h4>AI assessment, score {s.ai?.score?.toFixed(2)} ({s.ai?.model_version}), regime {s.ai?.regime?.toLowerCase()}</h4>
          <ul>
            {s.ai?.reasons_for?.map((r, i) => <li key={`f${i}`} className="ok"><span className="mark">✓</span><span className="check-detail">{r}</span></li>)}
            {s.ai?.reasons_against?.map((r, i) => <li key={`a${i}`} className="fail"><span className="mark">✕</span><span className="check-detail">{r}</span></li>)}
          </ul>
        </div>
        {s.risk && <Checks title={`Risk manager: ${s.risk.approved ? "approved" : "rejected"}`} checks={s.risk.checks} />}
      </div>
      <p className="muted small">Signal {s.signal_id} from {s.strategy_id} {s.strategy_version} in {s.mode.toLowerCase()} mode</p>
    </div>
  );
}

export default function Signals() {
  const [symbol, setSymbol] = useState("");
  const [status, setStatus] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const q = new URLSearchParams({ limit: "100", ...(symbol && { symbol }), ...(status && { status }) });
  const { data, error } = usePoll<Paged<SignalRow>>(`/signals/?${q}`, 5000);

  return (
    <Section
      title="Trading signals"
      aside={
        <div className="filters">
          <input placeholder="Symbol" value={symbol} onChange={(e) => setSymbol(e.target.value.toUpperCase())} aria-label="Filter by symbol" />
          <select value={status} onChange={(e) => setStatus(e.target.value)} aria-label="Filter by status">
            {STATUSES.map((s) => <option key={s} value={s}>{s ? s.replace(/_/g, " ").toLowerCase() : "Any status"}</option>)}
          </select>
        </div>
      }
    >
      <ErrorNote error={error} />
      {data?.results.length === 0 && <Empty>No signals match these filters.</Empty>}
      {data && data.results.length > 0 && (
        <div className="scroll-x">
          <table className="table">
            <thead>
              <tr>
                <th>Time</th><th>Symbol</th><th>Side</th><th>TF</th><th className="num">Entry</th><th className="num">Stop loss</th>
                <th className="num">Take profit</th><th className="num">RR</th><th className="num">Risk</th><th className="num">AI</th><th>Status</th>
              </tr>
            </thead>
            <tbody>
              {data.results.map((s) => (
                <Fragment key={s.signal_id}>
                  <tr className="clickable" onClick={() => setOpen(open === s.signal_id ? null : s.signal_id)}
                      tabIndex={0} onKeyDown={(e) => e.key === "Enter" && setOpen(open === s.signal_id ? null : s.signal_id)} aria-expanded={open === s.signal_id}>
                    <td>{time(s.created_at)}</td><td>{s.symbol}</td><td><Side side={s.side} /></td><td>{s.timeframe}</td>
                    <td className="num">{price(s.entry)}</td><td className="num">{price(s.stop_loss)}</td><td className="num">{price(s.take_profit)}</td>
                    <td className="num">{s.rr.toFixed(2)}</td>
                    <td className="num">{s.risk_money ? `${money(s.risk_money)} (${pct(s.risk_pct)})` : "–"}</td>
                    <td className="num">{s.ai_score?.toFixed(2) ?? "–"}</td><td><Status value={s.status} /></td>
                  </tr>
                  {open === s.signal_id && (
                    <tr className="detail-row"><td colSpan={11}><Detail s={s} /></td></tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Section>
  );
}
