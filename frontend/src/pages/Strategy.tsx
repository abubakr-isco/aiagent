import { patch, type Paged } from "../api";
import { FieldSpec, SettingsForm } from "../components/SettingsForm";
import { useConsole } from "../components/Shell";
import { ErrorNote, Section, Status } from "../components/ui";
import { usePoll } from "../hooks";

interface StrategyRow {
  id: number;
  strategy_id: string;
  version: string;
  params: Record<string, unknown>;
  enabled: boolean;
  validation_status: string;
  validation_run: number | null;
  updated_by: string | null;
}

const TF = ["M5", "M15", "M30", "H1", "H4"];
const FIELDS: FieldSpec[] = [
  { key: "trend_tf", label: "Trend timeframe", type: "select", options: TF },
  { key: "entry_tf", label: "Entry timeframe", type: "select", options: TF },
  { key: "context_tf", label: "Context timeframe", type: "select", options: TF },
  { key: "pullback_ema", label: "Pullback to", type: "select", options: ["fast", "mid"], hint: "fast is EMA 20, mid is EMA 50" },
  { key: "pullback_lookback", label: "Pullback window, bars", type: "number", step: 1 },
  { key: "pullback_tolerance_atr", label: "Touch tolerance, ATR", type: "number" },
  { key: "max_pullback_depth_atr", label: "Maximum depth below EMA 50, ATR", type: "number" },
  { key: "rsi_long_min", label: "RSI range for buys, from", type: "number" },
  { key: "rsi_long_max", label: "RSI range for buys, to", type: "number" },
  { key: "rsi_short_min", label: "RSI range for sells, from", type: "number" },
  { key: "rsi_short_max", label: "RSI range for sells, to", type: "number" },
  { key: "require_macd_confirmation", label: "Require MACD momentum", type: "boolean" },
  { key: "sl_atr_buffer", label: "Stop buffer beyond swing, ATR", type: "number" },
  { key: "min_sl_atr", label: "Minimum stop distance, ATR", type: "number" },
  { key: "max_sl_atr", label: "Maximum stop distance, ATR", type: "number" },
  { key: "rr_target", label: "Take profit, R", type: "number" },
  { key: "min_room_rr", label: "Room to next level, R", type: "number" },
  { key: "use_context_filter", label: "Skip trades against the context trend", type: "boolean" },
  { key: "block_high_volatility", label: "Skip extreme volatility", type: "boolean" },
  { key: "session_start_utc", label: "Session start, UTC hour", type: "number", step: 1 },
  { key: "session_end_utc", label: "Session end, UTC hour", type: "number", step: 1 },
  { key: "max_spread_atr_ratio", label: "Maximum spread, share of ATR", type: "number" },
  { key: "exit_on_trend_flip", label: "Exit when the trend flips", type: "boolean" },
  { key: "max_hold_bars", label: "Maximum holding time, entry bars", type: "number", step: 1 },
  { key: "breakeven_at_r", label: "Move stop to breakeven at, R (0 off)", type: "number" },
];

export default function Strategy() {
  const { can } = useConsole();
  const { data, error, reload } = usePoll<Paged<StrategyRow>>("/strategies/", 0);
  return (
    <>
      <ErrorNote error={error} />
      {data?.results.map((s) => (
        <Section key={s.id} title={`${s.strategy_id} ${s.version}`} aside={<Status value={s.validation_status} />}>
          <p className="muted">
            Trend on {String(s.params.trend_tf)}, pullback entry on {String(s.params.entry_tf)}, stop behind the pullback swing, target at a fixed
            reward to risk. Profitability is not assumed: live trading needs a passed walk-forward test on real broker data.
          </p>
          <SettingsForm
            fields={FIELDS}
            values={s.params}
            editable={can("admin")}
            warning="Changing parameters resets validation to not validated. Run a new walk-forward test before trading live."
            onSave={async (changed) => {
              await patch(`/strategies/${s.id}/`, { params: changed });
              await reload();
            }}
          />
        </Section>
      ))}
    </>
  );
}
