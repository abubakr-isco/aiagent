import { useState } from "react";

import type { Metrics, Paged, TradeRow } from "../api";
import { Empty, ErrorNote, Section, Side, Status } from "../components/ui";
import { money, price, signed, time, tone } from "../format";
import { usePoll } from "../hooks";

export function MetricsRow({ m, currency = "" }: { m: Metrics | null | undefined; currency?: string }) {
  if (!m) return null;
  return (
    <div className="ledger ledger-small">
      <div><span>Trades</span><strong>{m.trades}</strong></div>
      <div><span>Win rate</span><strong>{m.win_rate.toFixed(1)}%</strong></div>
      <div><span>Profit factor</span><strong>{m.profit_factor === null ? "–" : m.profit_factor.toFixed(2)}</strong></div>
      <div><span>Expectancy</span><strong className={tone(m.expectancy)}>{signed(m.expectancy)} {currency}</strong><small>{m.expectancy_r === null ? "" : `${signed(m.expectancy_r, 2)} R`}</small></div>
      <div><span>Average win / loss</span><strong>{money(m.avg_win)} / {money(m.avg_loss)}</strong></div>
      <div><span>Max drawdown</span><strong>{money(m.max_drawdown)}</strong><small>{m.max_drawdown_pct === null ? "" : `${m.max_drawdown_pct.toFixed(2)}%`}</small></div>
      <div><span>Net result</span><strong className={tone(m.net_profit)}>{signed(m.net_profit)}</strong><small>costs {money(m.total_costs)}</small></div>
    </div>
  );
}

interface Report {
  source: string;
  overall: Metrics;
  groups?: Record<string, Metrics>;
}

export default function History() {
  const [source, setSource] = useState<"DEMO" | "LIVE">("DEMO");
  const [groupBy, setGroupBy] = useState<"symbol" | "strategy">("symbol");
  const trades = usePoll<Paged<TradeRow>>(`/trades/?source=${source}&limit=200`, 8000);
  const report = usePoll<Report>(`/analytics/report/?source=${source}&group_by=${groupBy}`, 15000);

  return (
    <>
      <div className="tabs" role="tablist" aria-label="Account type">
        {(["DEMO", "LIVE"] as const).map((s) => (
          <button key={s} role="tab" aria-selected={source === s} className={source === s ? "active" : ""} onClick={() => setSource(s)}>
            {s === "DEMO" ? "Demo account" : "Live account"}
          </button>
        ))}
      </div>
      <Section title="Performance" aside={
        <select value={groupBy} onChange={(e) => setGroupBy(e.target.value as "symbol" | "strategy")} aria-label="Group by">
          <option value="symbol">By symbol</option><option value="strategy">By strategy</option>
        </select>
      }>
        <p className="muted">Closed trades only. Demo, live and backtest results are always reported separately.</p>
        <ErrorNote error={report.error} />
        <MetricsRow m={report.data?.overall} />
        {report.data?.groups && Object.keys(report.data.groups).length > 0 && (
          <table className="table table-compact">
            <thead><tr><th>{groupBy === "symbol" ? "Symbol" : "Strategy"}</th><th className="num">Trades</th><th className="num">Win rate</th><th className="num">Profit factor</th><th className="num">Expectancy R</th><th className="num">Net</th></tr></thead>
            <tbody>
              {Object.entries(report.data.groups).map(([k, m]) => (
                <tr key={k}><td>{k}</td><td className="num">{m.trades}</td><td className="num">{m.win_rate.toFixed(1)}%</td>
                  <td className="num">{m.profit_factor?.toFixed(2) ?? "–"}</td><td className="num">{m.expectancy_r?.toFixed(2) ?? "–"}</td>
                  <td className={`num ${tone(m.net_profit)}`}>{signed(m.net_profit)}</td></tr>
              ))}
            </tbody>
          </table>
        )}
      </Section>
      <Section title="Orders and trades">
        <ErrorNote error={trades.error} />
        {trades.data?.results.length === 0 && <Empty>No orders on the {source.toLowerCase()} account yet.</Empty>}
        {trades.data && trades.data.results.length > 0 && (
          <div className="scroll-x">
            <table className="table">
              <thead>
                <tr><th>Created</th><th>Symbol</th><th>Side</th><th className="num">Lots</th><th className="num">Entry</th><th className="num">Exit</th>
                  <th className="num">Commission + swap</th><th className="num">P&amp;L</th><th>Exit reason</th><th>Status</th></tr>
              </thead>
              <tbody>
                {trades.data.results.map((t) => (
                  <tr key={t.signal_id} title={t.message}>
                    <td>{time(t.created_at)}</td><td>{t.symbol}</td><td><Side side={t.side} /></td>
                    <td className="num">{t.filled_volume || t.requested_volume}</td><td className="num">{price(t.fill_price)}</td><td className="num">{price(t.exit_price)}</td>
                    <td className="num">{money(t.commission + t.swap)}</td>
                    <td className={`num ${tone(t.realized_pnl ?? t.unrealized_pnl)}`}>{signed(t.realized_pnl ?? t.unrealized_pnl)}{t.realized_pnl === null && t.unrealized_pnl !== null ? " open" : ""}</td>
                    <td>{t.close_reason || (t.status === "REJECTED" ? t.message : "–")}</td><td><Status value={t.status} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Section>
    </>
  );
}
