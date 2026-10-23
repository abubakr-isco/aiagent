import { useState } from "react";

import { post, put } from "../api";
import { FieldSpec, SettingsForm } from "../components/SettingsForm";
import { useConsole } from "../components/Shell";
import { Symbols } from "../components/Symbols";
import { Confirm, ErrorNote, Section } from "../components/ui";
import { ago, money } from "../format";
import { usePoll } from "../hooks";

interface ConnectionPayload {
  settings: Record<string, unknown>;
  status: { runner_alive: boolean; connected: boolean; gateway: string; account: Record<string, unknown> | null; heartbeat_at: string | null; last_error: string };
  secrets_configured: Record<string, boolean>;
}

const FIELDS: FieldSpec[] = [
  { key: "gateway", label: "Terminal access", type: "select", options: ["mock", "mt5", "remote"], hint: "mock simulates a terminal; mt5 needs Windows; remote uses the bridge on a Windows host. Restart the runner after changing." },
  { key: "bridge_url", label: "Bridge address", type: "text", hint: "https:// address of the bridge, or http://127.0.0.1 through an SSH or VPN tunnel." },
  { key: "loop_interval_s", label: "Cycle interval, seconds", type: "number" },
  { key: "bars_per_timeframe", label: "Bars loaded per timeframe", type: "number", step: 1 },
  { key: "ai_min_quality", label: "Minimum AI quality score, 0 to 1", type: "number" },
];

const MODES = [
  { mode: "ANALYSIS", title: "Analysis", text: "Analyse and record signals. Never sends orders." },
  { mode: "DEMO", title: "Demo", text: "Trade only when the terminal reports a demo account." },
  { mode: "LIVE", title: "Live", text: "Real money. Needs the server flag, a validated strategy, a real account, your password and a typed confirmation." },
] as const;

export default function Connection() {
  const { can, status } = useConsole();
  const { data, error, reload } = usePoll<ConnectionPayload>("/connection/", 5000);
  const [confirm, setConfirm] = useState<"DEMO" | "LIVE" | "ANALYSIS" | null>(null);
  const acct = data?.status.account;

  return (
    <>
      <ErrorNote error={error} />
      {data && (
        <Section title="Terminal">
          <dl className="facts">
            <div><dt>Runner</dt><dd className={data.status.runner_alive ? "" : "neg"}>{data.status.runner_alive ? `running, heartbeat ${ago(data.status.heartbeat_at)}` : "not running"}</dd></div>
            <div><dt>Gateway</dt><dd>{data.status.gateway || "–"}{data.status.gateway === "mock" ? ", simulated terminal, not a real MT5 connection" : ""}</dd></div>
            <div><dt>Connection</dt><dd className={data.status.connected ? "pos" : "neg"}>{data.status.connected ? "connected" : "disconnected"}</dd></div>
            <div><dt>Account</dt><dd>{acct ? `${String(acct.login)} on ${String(acct.server)}, ${String(acct.kind).toLowerCase()}, equity ${money(Number(acct.equity))} ${String(acct.currency)}` : "–"}</dd></div>
            {data.status.last_error && <div><dt>Last error</dt><dd className="neg">{data.status.last_error}</dd></div>}
          </dl>
          <h3 className="sub">Credentials</h3>
          <p className="muted">
            Login, password and bridge token are read from environment variables on the server and are never shown or stored by this console.
          </p>
          <ul className="secrets">
            {Object.entries(data.secrets_configured).map(([k, v]) => <li key={k} className={v ? "ok" : ""}>{k}: {v ? "set" : "not set"}</li>)}
          </ul>
        </Section>
      )}

      <Section title="Trading mode">
        <div className="modes" role="radiogroup" aria-label="Trading mode">
          {MODES.map((m) => {
            const active = status?.mode === m.mode;
            const disabled = !can("admin") || active || (m.mode === "LIVE" && !status?.allow_live_env);
            return (
              <button key={m.mode} role="radio" aria-checked={active} className={`mode mode-${m.mode.toLowerCase()} ${active ? "active" : ""}`} disabled={disabled} onClick={() => setConfirm(m.mode)}>
                <strong>{m.title}{active ? ", current" : ""}</strong>
                <span>{m.text}</span>
                {m.mode === "LIVE" && !status?.allow_live_env && <small>Disabled on this server (ALLOW_LIVE_TRADING is off).</small>}
              </button>
            );
          })}
        </div>
        <p className="muted small">Changing mode pauses the agent. Press Start afterwards to allow new entries.</p>
      </Section>

      <Section title="Symbols">
        <p className="muted">Use the exact names from your terminal's Market Watch. Changes apply on the next cycle.</p>
        <Symbols editable={can("admin")} />
      </Section>

      {data && (
        <Section title="Connection settings">
          <SettingsForm fields={FIELDS} values={data.settings} editable={can("admin")} onSave={async (changed) => { await put("/connection/", changed); await reload(); }} />
        </Section>
      )}

      {confirm && (
        <Confirm
          danger={confirm === "LIVE"}
          title={confirm === "LIVE" ? "Switch to live trading with real money?" : `Switch to ${confirm.toLowerCase()} mode?`}
          body={confirm === "LIVE"
            ? <p>The agent will be able to open real positions within the risk limits. Losses are real. The server checks the account type, the strategy validation and your password before applying it.</p>
            : <p>{MODES.find((m) => m.mode === confirm)?.text}</p>}
          phrase={confirm === "LIVE" ? "ENABLE LIVE TRADING" : undefined}
          password={confirm === "LIVE"}
          confirmLabel={confirm === "LIVE" ? "Enable live trading" : `Switch to ${confirm.toLowerCase()}`}
          onConfirm={async ({ phrase, password }) => { await post("/control/mode/", { mode: confirm, confirm: phrase, password }); }}
          onClose={() => setConfirm(null)}
        />
      )}
    </>
  );
}
