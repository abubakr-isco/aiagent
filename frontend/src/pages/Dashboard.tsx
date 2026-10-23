import { Link } from "react-router-dom";

import { useConsole } from "../components/Shell";
import { Empty, Section, Side, Status } from "../components/ui";
import { money, pct, price, signed, time, tone } from "../format";
import { PositionsTable } from "./Positions";

export default function Dashboard() {
  const { status } = useConsole();
  if (!status) return null;
  const a = status.account ?? {};
  const cur = a.currency ?? "";
  const day = status.day ?? {};

  return (
    <>
      <div className="ledger" aria-label="Account">
        <div><span>Balance, {cur}</span><strong>{money(a.balance)}</strong></div>
        <div><span>Equity, {cur}</span><strong>{money(a.equity)}</strong></div>
        <div><span>Today, {cur}</span><strong className={tone(day.pnl)}>{signed(day.pnl)}</strong><small>from {money(day.start_equity)}</small></div>
        <div><span>Daily loss used</span><strong className={day.locked ? "neg" : ""}>{pct(day.loss_pct)}</strong><small>{day.locked ? "limit reached, locked until tomorrow" : "of start equity"}</small></div>
        <div><span>Floating drawdown</span><strong>{pct(status.floating_drawdown_pct)}</strong></div>
        <div><span>Free margin, {cur}</span><strong>{money(a.margin_free)}</strong><small>{a.kind ? `${a.kind.toLowerCase()} account ${a.login ?? ""}` : ""}</small></div>
      </div>

      {status.strategy && status.strategy.validation_status !== "PASSED" && (
        <p className="caution">
          Strategy {status.strategy.id} {status.strategy.version} has not passed walk-forward validation on real data. Live trading stays locked.{" "}
          <Link to="/backtests">Run a validation</Link>
        </p>
      )}

      <Section title="Open positions" aside={<Link to="/positions">All positions</Link>}>
        <PositionsTable positions={status.positions} currency={cur} />
      </Section>

      <div className="split">
        <Section title="Latest signals" aside={<Link to="/signals">All signals</Link>}>
          {status.recent_signals.length === 0 ? (
            <Empty>No setups yet. The agent records every signal here, including the ones it declines.</Empty>
          ) : (
            <table className="table">
              <thead><tr><th>Time</th><th>Symbol</th><th>Side</th><th className="num">Entry</th><th className="num">AI</th><th>Status</th></tr></thead>
              <tbody>
                {status.recent_signals.map((s) => (
                  <tr key={s.signal_id}>
                    <td>{time(s.created_at)}</td><td>{s.symbol}</td><td><Side side={s.side} /></td>
                    <td className="num">{price(s.entry)}</td><td className="num">{s.ai_score?.toFixed(2) ?? "–"}</td><td><Status value={s.status} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Section>

        <Section title="Latest executions" aside={<Link to="/history">Trade history</Link>}>
          {status.recent_trades.length === 0 ? (
            <Empty>No orders sent yet.</Empty>
          ) : (
            <table className="table">
              <thead><tr><th>Time</th><th>Symbol</th><th>Side</th><th className="num">Lots</th><th className="num">P&amp;L</th><th>Status</th></tr></thead>
              <tbody>
                {status.recent_trades.map((t) => (
                  <tr key={t.signal_id}>
                    <td>{time(t.created_at)}</td><td>{t.symbol}</td><td><Side side={t.side} /></td>
                    <td className="num">{t.filled_volume || t.requested_volume}</td>
                    <td className={`num ${tone(t.realized_pnl ?? t.unrealized_pnl)}`}>{signed(t.realized_pnl ?? t.unrealized_pnl)}</td>
                    <td><Status value={t.status} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Section>
      </div>

      <Section title="Agent journal" aside={<Link to="/logs">System logs</Link>}>
        <ol className="journal">
          {status.events.map((e, i) => (
            <li key={i} className={`lvl-${e.level.toLowerCase()}`}>
              <time>{time(e.time)}</time>
              <span className="cat">{e.category}</span>
              <span>{e.message}</span>
            </li>
          ))}
        </ol>
      </Section>
    </>
  );
}
