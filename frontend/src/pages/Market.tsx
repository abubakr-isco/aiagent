import type { Check, Paged } from "../api";
import { Checks, Empty, ErrorNote, Section } from "../components/ui";
import { price, time } from "../format";
import { usePoll } from "../hooks";

interface Frame {
  timeframe: string;
  bar_time: string;
  close: number;
  ema_fast: number;
  ema_mid: number;
  ema_slow: number;
  rsi: number;
  macd_hist: number;
  atr: number;
  atr_rank: number;
  adx: number;
  trend: string;
  structure: string;
  structure_labels: string[];
  support: number | null;
  resistance: number | null;
  breakout: string | null;
  retest: boolean;
  volume_kind: string | null;
  volume_ratio: number | null;
}

interface Snapshot {
  symbol: string;
  time: string;
  bid: number | null;
  ask: number | null;
  data: { frames: Record<string, Frame>; spread: number | null };
  evaluation: { checks?: Check[]; has_signal?: boolean; side_considered?: string | null; rejection_reasons?: string[] };
  updated_at: string;
}

const ORDER = ["H4", "H1", "M15", "M5"];

export default function Market() {
  const { data, error } = usePoll<Paged<Snapshot>>("/market/snapshots/", 5000);
  return (
    <>
      <ErrorNote error={error} />
      {data && data.results.length === 0 && <Empty>No analysis yet. Start the agent runner to analyse the configured symbols.</Empty>}
      {data?.results.map((s) => (
        <Section key={s.symbol} title={s.symbol} aside={<span className="muted">Bid {price(s.bid)}, ask {price(s.ask)}, updated {time(s.updated_at)}</span>}>
          <div className="scroll-x">
            <table className="table table-compact">
              <thead>
                <tr>
                  <th>Timeframe</th><th>Trend</th><th>Structure</th><th className="num">Close</th><th className="num">EMA 20 / 50 / 200</th>
                  <th className="num">RSI</th><th className="num">ADX</th><th className="num">ATR rank</th><th className="num">Support</th><th className="num">Resistance</th><th>Breakout</th>
                </tr>
              </thead>
              <tbody>
                {ORDER.filter((tf) => s.data.frames?.[tf]).map((tf) => {
                  const f = s.data.frames[tf];
                  return (
                    <tr key={tf}>
                      <td>{tf}</td>
                      <td className={f.trend === "UP" ? "pos" : f.trend === "DOWN" ? "neg" : ""}>{f.trend.toLowerCase()}</td>
                      <td>{f.structure.toLowerCase()} {f.structure_labels.join("/")}</td>
                      <td className="num">{price(f.close)}</td>
                      <td className="num">{price(f.ema_fast)} / {price(f.ema_mid)} / {price(f.ema_slow)}</td>
                      <td className="num">{f.rsi?.toFixed(1)}</td>
                      <td className="num">{f.adx?.toFixed(1)}</td>
                      <td className="num">{f.atr_rank?.toFixed(2)}</td>
                      <td className="num">{price(f.support)}</td>
                      <td className="num">{price(f.resistance)}</td>
                      <td>{f.breakout ? `${f.breakout.toLowerCase()}${f.retest ? ", retested" : ""}` : "–"}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <p className="muted small">
            {s.data.frames?.M15?.volume_kind === "tick" ? "Volume shown is tick volume (no exchange volume for this instrument); it is informational only." : ""}
          </p>
          {s.evaluation?.checks && (
            <Checks
              title={s.evaluation.has_signal ? `Setup found (${s.evaluation.side_considered?.toLowerCase()})` : "Why there is no setup right now"}
              checks={s.evaluation.checks}
            />
          )}
        </Section>
      ))}
    </>
  );
}
