import { Fragment, useState } from "react";

import { post, type Metrics, type Paged } from "../api";
import { useConsole } from "../components/Shell";
import { Empty, ErrorNote, Section, Sparkline, Status } from "../components/ui";
import { time } from "../format";
import { usePoll } from "../hooks";
import { MetricsRow } from "./History";

interface Run {
  id: number;
  kind: string;
  symbol: string;
  status: string;
  data_source: string;
  data_path: string;
  base_timeframe: string;
  created_at: string;
  metrics: Metrics | null;
  validation_status: string;
  reasons: string[];
  synthetic: boolean;
  error: string;
  costs: Record<string, number | null>;
}

interface RunDetail extends Run {
  result: {
    equity_curve?: [string, number][];
    folds?: { fold: number; test_start: string; test_end: string; chosen_params: Record<string, unknown>; test_metrics: Metrics }[];
    oos_trades?: { pnl: number }[];
    rejections?: Record<string, number>;
    signals?: number;
    ai_rejected?: number;
  } | null;
}

function NewRun({ onCreated }: { onCreated: () => void }) {
  const [form, setForm] = useState({ kind: "WALK_FORWARD", symbol: "EURUSD", data_source: "csv", data_path: "EURUSD_M15.csv", base_timeframe: "M15",
    spread_points: "", commission: "3.5", slippage: "1", grid: '[{"rr_target": 1.5}, {"rr_target": 2.0}, {"rr_target": 2.5}]' });
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const set = (k: string) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => setForm({ ...form, [k]: e.target.value });

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await post("/backtests/", {
        kind: form.kind, symbol: form.symbol, data_source: form.data_source, data_path: form.data_path, base_timeframe: form.base_timeframe,
        param_grid: form.kind === "WALK_FORWARD" ? JSON.parse(form.grid || "[]") : [],
        costs: { spread_points: form.spread_points === "" ? null : Number(form.spread_points), commission_per_lot_side: Number(form.commission), slippage_points: Number(form.slippage) },
      });
      onCreated();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="settings" onSubmit={submit}>
      <div className="settings-grid">
        <label className="field"><span>Test</span><select value={form.kind} onChange={set("kind")}><option value="WALK_FORWARD">Walk-forward (validation)</option><option value="SINGLE">Single run</option></select></label>
        <label className="field"><span>Symbol</span><input value={form.symbol} onChange={set("symbol")} /></label>
        <label className="field"><span>Data</span><select value={form.data_source} onChange={set("data_source")}>
          <option value="csv">CSV exported from MT5</option><option value="gateway">History from the connected terminal</option><option value="synthetic">Synthetic (pipeline check only)</option>
        </select></label>
        {form.data_source === "csv" && <label className="field"><span>File in the data folder</span><input value={form.data_path} onChange={set("data_path")} /></label>}
        <label className="field"><span>Bar timeframe</span><select value={form.base_timeframe} onChange={set("base_timeframe")}><option>M5</option><option>M15</option></select></label>
        <label className="field"><span>Spread, points</span><input value={form.spread_points} placeholder="from bars" onChange={set("spread_points")} /></label>
        <label className="field"><span>Commission per lot per side</span><input value={form.commission} onChange={set("commission")} /></label>
        <label className="field"><span>Slippage, points</span><input value={form.slippage} onChange={set("slippage")} /></label>
        {form.kind === "WALK_FORWARD" && <label className="field field-wide"><span>Parameter sets to compare (JSON, at most 12)</span><input value={form.grid} onChange={set("grid")} spellCheck={false} /></label>}
      </div>
      <ErrorNote error={error} />
      <div className="settings-actions"><button className="btn btn-primary" disabled={busy}>{busy ? "Starting…" : "Run test"}</button></div>
    </form>
  );
}

function RunView({ id }: { id: number }) {
  const { data } = usePoll<RunDetail>(`/backtests/${id}/`, 4000);
  if (!data) return null;
  const curve = data.result?.equity_curve?.map(([, v]) => v) ?? (data.result?.oos_trades ?? []).reduce<number[]>((acc, t) => [...acc, (acc.at(-1) ?? 10000) + t.pnl], [10000]);
  return (
    <div className="detail">
      {data.synthetic && <p className="caution">Synthetic data. This run only checks that the pipeline works. It says nothing about real profitability.</p>}
      {data.error && <ErrorNote error={data.error} />}
      <MetricsRow m={data.metrics} />
      <Sparkline points={curve} />
      {data.reasons?.length > 0 && <ul className="reasons">{data.reasons.map((r) => <li key={r}>{r}</li>)}</ul>}
      {data.result?.folds && (
        <table className="table table-compact">
          <thead><tr><th>Fold</th><th>Out-of-sample period</th><th>Chosen parameters</th><th className="num">Trades</th><th className="num">Profit factor</th><th className="num">Expectancy R</th></tr></thead>
          <tbody>
            {data.result.folds.map((f) => (
              <tr key={f.fold}><td>{f.fold}</td><td>{f.test_start.slice(0, 10)} to {f.test_end.slice(0, 10)}</td><td>{JSON.stringify(f.chosen_params)}</td>
                <td className="num">{f.test_metrics.trades}</td><td className="num">{f.test_metrics.profit_factor?.toFixed(2) ?? "–"}</td><td className="num">{f.test_metrics.expectancy_r?.toFixed(2) ?? "–"}</td></tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

export default function Backtests() {
  const { can } = useConsole();
  const { data, error, reload } = usePoll<Paged<Run>>("/backtests/?limit=50", 5000);
  const [open, setOpen] = useState<number | null>(null);
  return (
    <>
      {can("operator") && (
        <Section title="New test">
          <p className="muted">
            Fills happen at the next bar open with spread, commission and slippage. When stop and target fall in the same bar, the stop is assumed hit first.
            Only a walk-forward run on real data can validate the strategy for live trading.
          </p>
          <NewRun onCreated={reload} />
        </Section>
      )}
      <Section title="Runs">
        <ErrorNote error={error} />
        {data?.results.length === 0 && <Empty>No tests yet.</Empty>}
        {data && data.results.length > 0 && (
          <table className="table">
            <thead><tr><th>Started</th><th>Test</th><th>Symbol</th><th>Data</th><th className="num">Trades</th><th className="num">Expectancy R</th><th>Result</th></tr></thead>
            <tbody>
              {data.results.map((r) => (
                <Fragment key={r.id}>
                  <tr className="clickable" tabIndex={0} onClick={() => setOpen(open === r.id ? null : r.id)} onKeyDown={(e) => e.key === "Enter" && setOpen(open === r.id ? null : r.id)}>
                    <td>{time(r.created_at)}</td><td>{r.kind === "WALK_FORWARD" ? "Walk-forward" : "Single"}</td><td>{r.symbol}</td>
                    <td>{r.data_source === "csv" ? r.data_path : r.data_source}</td><td className="num">{r.metrics?.trades ?? "–"}</td>
                    <td className="num">{r.metrics?.expectancy_r?.toFixed(2) ?? "–"}</td>
                    <td><Status value={r.status === "DONE" ? r.validation_status || "DONE" : r.status} /></td>
                  </tr>
                  {open === r.id && <tr className="detail-row"><td colSpan={7}><RunView id={r.id} /></td></tr>}
                </Fragment>
              ))}
            </tbody>
          </table>
        )}
      </Section>
    </>
  );
}
