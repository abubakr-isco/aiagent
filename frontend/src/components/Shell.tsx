import { createContext, ReactNode, useContext, useState } from "react";
import { NavLink } from "react-router-dom";

import { post, type Session, type Status } from "../api";
import { ago } from "../format";
import { usePoll } from "../hooks";
import { Confirm } from "./ui";

interface Ctx {
  session: Session;
  status: Status | null;
  statusError: string | null;
  reloadStatus: () => void;
  can: (role: "operator" | "admin") => boolean;
}

const ConsoleContext = createContext<Ctx | null>(null);

export function useConsole(): Ctx {
  const ctx = useContext(ConsoleContext);
  if (!ctx) throw new Error("useConsole outside Shell");
  return ctx;
}

const NAV = [
  ["/", "Dashboard"],
  ["/market", "Market analysis"],
  ["/signals", "Trading signals"],
  ["/positions", "Open positions"],
  ["/history", "Trade history"],
  ["/risk", "Risk management"],
  ["/strategy", "Strategy settings"],
  ["/backtests", "Backtesting"],
  ["/logs", "System logs"],
  ["/connection", "Connection"],
] as const;

const RANK = { viewer: 0, operator: 1, admin: 2 };

const MODE_TEXT: Record<string, string> = {
  ANALYSIS: "Analysis only. No orders are sent.",
  DEMO: "Demo trading on a verified demo account.",
  LIVE: "Live trading with real money.",
};

function SafetyStrip({ status, error, can, onCommand }: { status: Status | null; error: string | null; can: Ctx["can"]; onCommand: (c: string) => void }) {
  if (!status) {
    return <div className="strip strip-unknown"><span>{error ?? "Loading agent status…"}</span></div>;
  }
  const mode = status.kill_switch ? "EMERGENCY" : status.mode;
  const trading = status.blocked_reasons.length === 0;
  return (
    <div className={`strip strip-${mode.toLowerCase()}`} role="status" aria-live="polite">
      <div className="strip-mode">
        <strong>{status.kill_switch ? "Emergency stop" : status.mode.charAt(0) + status.mode.slice(1).toLowerCase()}</strong>
        <span>{status.kill_switch ? "Kill switch engaged. New trades are blocked." : MODE_TEXT[status.mode]}</span>
      </div>
      <dl className="strip-facts">
        <div><dt>Agent</dt><dd>{status.runner_alive ? status.state.replace("_", " ").toLowerCase() : "runner offline"}</dd></div>
        <div>
          <dt>Terminal</dt>
          <dd className={status.connected ? "" : "neg"}>{status.connected ? (status.is_mock ? "mock (simulated)" : "connected") : "disconnected"}</dd>
        </div>
        <div><dt>Heartbeat</dt><dd>{ago(status.heartbeat_at)}</dd></div>
        <div><dt>New trades</dt><dd className={trading ? "pos" : ""}>{trading ? "allowed" : "blocked"}</dd></div>
      </dl>
      {can("operator") && (
        <div className="strip-actions">
          <button className="btn" onClick={() => onCommand("start")} disabled={status.kill_switch || status.state === "RUNNING"}>Start</button>
          <button className="btn" onClick={() => onCommand("pause")} disabled={status.kill_switch || status.state === "PAUSED"}>Pause</button>
          <button className="btn" onClick={() => onCommand("stop")} disabled={status.state === "STOPPED"}>Stop</button>
          <button className="btn btn-emergency" onClick={() => onCommand("emergency_stop")} disabled={status.kill_switch}>Emergency stop</button>
        </div>
      )}
    </div>
  );
}

export function Shell({ session, onLogout, children }: { session: Session; onLogout: () => void; children: ReactNode }) {
  const { data: status, error, reload } = usePoll<Status>("/status/", 3000);
  const [pending, setPending] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const can = (role: "operator" | "admin") => RANK[session.role] >= RANK[role];

  async function send(command: string, extra: Record<string, unknown> = {}) {
    await post("/control/command/", { command, ...extra });
    setNotice(`${command.replace("_", " ")} sent. The agent applies it on its next cycle.`);
    reload();
  }

  return (
    <ConsoleContext.Provider value={{ session, status, statusError: error, reloadStatus: reload, can }}>
      <div className="app">
        <aside className="sidebar">
          <div className="brand">Agent console</div>
          <nav aria-label="Sections">
            {NAV.map(([to, label]) => (
              <NavLink key={to} to={to} end={to === "/"} className={({ isActive }) => (isActive ? "active" : "")}>{label}</NavLink>
            ))}
          </nav>
          <div className="whoami">
            <span>{session.username}</span>
            <span className="role">{session.role}</span>
            <button className="link" onClick={onLogout}>Sign out</button>
          </div>
        </aside>
        <main className="main">
          <SafetyStrip status={status} error={error} can={can} onCommand={(c) => (c === "emergency_stop" ? setPending(c) : send(c).catch((e) => setNotice(e.message)))} />
          {status?.blocked_reasons?.length ? (
            <p className="blocked">Trading is blocked: {status.blocked_reasons.join(", ")}.</p>
          ) : null}
          {notice && <p className="notice" onClick={() => setNotice(null)}>{notice}</p>}
          <div className="content">{children}</div>
        </main>
      </div>
      {pending === "emergency_stop" && (
        <Confirm
          danger
          title="Stop all new trading?"
          body={<p>The kill switch blocks every new order and cancels the agent's pending orders. Open positions stay open with their stop losses. Only an admin can reset it.</p>}
          confirmLabel="Engage emergency stop"
          onConfirm={() => send("emergency_stop", { reason: `dashboard (${session.username})` })}
          onClose={() => setPending(null)}
        />
      )}
    </ConsoleContext.Provider>
  );
}
