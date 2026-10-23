import { put } from "../api";
import { FieldSpec, SettingsForm } from "../components/SettingsForm";
import { useConsole } from "../components/Shell";
import { Empty, ErrorNote, Section } from "../components/ui";
import { money } from "../format";
import { usePoll } from "../hooks";

const FIELDS: FieldSpec[] = [
  { key: "risk_per_trade_pct", label: "Risk per trade, % of equity", type: "number", hint: "Position size is calculated from the stop distance so a stop-out loses at most this much." },
  { key: "max_total_open_risk_pct", label: "Total open risk, % of equity", type: "number" },
  { key: "daily_loss_limit_pct", label: "Daily loss limit, % of start equity", type: "number", hint: "Includes floating P&L, commission and swap. When hit, new trades stop until the next trading day." },
  { key: "daily_limit_includes_open_risk", label: "Count open stops toward the daily limit", type: "boolean", hint: "Refuse a trade if all stops being hit could breach the daily limit." },
  { key: "max_open_positions", label: "Maximum open positions", type: "number", step: 1 },
  { key: "min_rr", label: "Minimum reward to risk", type: "number" },
  { key: "max_spread_atr_ratio", label: "Maximum spread, share of ATR", type: "number" },
  { key: "spread_anomaly_factor", label: "Spread anomaly factor vs median", type: "number" },
  { key: "max_quote_age_s", label: "Stale quote after, seconds", type: "number" },
  { key: "max_margin_usage_pct", label: "Maximum margin used, % of equity", type: "number" },
  { key: "min_margin_level_pct", label: "Minimum margin level after trade, %", type: "number" },
  { key: "risk_calc_tolerance_pct", label: "Risk cross-check tolerance, %", type: "number", hint: "order_calc_profit and tick value must agree within this tolerance or the trade is refused." },
  { key: "block_if_foreign_position_without_sl", label: "Block when a manual position has no stop loss", type: "boolean" },
  { key: "trading_day_utc_offset_hours", label: "Trading day offset from UTC, hours", type: "number", step: 1 },
];

interface DayRow { day: string; start_equity: number; locked: boolean; locked_reason: string }

export default function Risk() {
  const { can } = useConsole();
  const settings = usePoll<Record<string, unknown>>("/risk/settings/", 0);
  const days = usePoll<DayRow[]>("/risk/state/", 15000);

  return (
    <>
      <Section title="Risk limits">
        <p className="muted">
          Every order passes these checks first. Grids, averaging and martingale are not possible: one position per symbol, and the
          risk percentage cannot be raised right after a losing trade. The AI cannot change these values.
        </p>
        <ErrorNote error={settings.error} />
        {settings.data && (
          <SettingsForm
            fields={FIELDS}
            values={settings.data}
            editable={can("admin")}
            onSave={async (changed) => {
              await put("/risk/settings/", changed);
              await settings.reload();
            }}
          />
        )}
        {settings.data?.updated_by ? <p className="muted small">Last changed by {String(settings.data.updated_by)}.</p> : null}
      </Section>
      <Section title="Trading days">
        {days.data?.length === 0 && <Empty>No trading day recorded yet.</Empty>}
        {days.data && days.data.length > 0 && (
          <table className="table table-compact">
            <thead><tr><th>Day</th><th className="num">Start equity</th><th>Daily limit</th></tr></thead>
            <tbody>
              {days.data.map((d) => (
                <tr key={d.day}><td>{d.day}</td><td className="num">{money(d.start_equity)}</td><td className={d.locked ? "neg" : ""}>{d.locked ? d.locked_reason : "not reached"}</td></tr>
              ))}
            </tbody>
          </table>
        )}
      </Section>
    </>
  );
}
