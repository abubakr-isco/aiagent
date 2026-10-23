// Thin API client. The token lives in sessionStorage: it is gone when the tab closes.

const TOKEN_KEY = "agent-console-token";

export type Role = "viewer" | "operator" | "admin";

export interface Session {
  token: string;
  username: string;
  role: Role;
}

export class ApiError extends Error {
  status: number;
  body: unknown;
  constructor(status: number, body: unknown, message: string) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

export function loadSession(): Session | null {
  try {
    const raw = sessionStorage.getItem(TOKEN_KEY);
    return raw ? (JSON.parse(raw) as Session) : null;
  } catch {
    return null;
  }
}

export function saveSession(s: Session | null): void {
  try {
    if (s) sessionStorage.setItem(TOKEN_KEY, JSON.stringify(s));
    else sessionStorage.removeItem(TOKEN_KEY);
  } catch {
    /* storage unavailable: session lasts for this page only */
  }
}

function describe(body: unknown, status: number): string {
  if (body && typeof body === "object") {
    const b = body as Record<string, unknown>;
    if (Array.isArray(b.problems)) return (b.problems as string[]).join("; ");
    if (typeof b.detail === "string") return b.detail;
    return Object.entries(b)
      .map(([k, v]) => `${k}: ${Array.isArray(v) ? v.join(", ") : typeof v === "object" ? JSON.stringify(v) : String(v)}`)
      .join("; ");
  }
  return `Request failed with status ${status}`;
}

export async function api<T>(path: string, init: RequestInit & { json?: unknown } = {}): Promise<T> {
  const session = loadSession();
  const headers: Record<string, string> = { Accept: "application/json" };
  if (session) headers.Authorization = `Token ${session.token}`;
  let body = init.body;
  if (init.json !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(init.json);
  }
  const res = await fetch(`/api${path}`, { ...init, headers, body });
  const text = await res.text();
  const data = text ? JSON.parse(text) : null;
  if (res.status === 401) {
    saveSession(null);
    window.dispatchEvent(new Event("session-expired"));
  }
  if (!res.ok) throw new ApiError(res.status, data, describe(data, res.status));
  return data as T;
}

export const post = <T,>(path: string, json: unknown) => api<T>(path, { method: "POST", json });
export const put = <T,>(path: string, json: unknown) => api<T>(path, { method: "PUT", json });
export const patch = <T,>(path: string, json: unknown) => api<T>(path, { method: "PATCH", json });

export interface Paged<T> {
  count: number;
  results: T[];
}

// ---- domain types (subset of what the backend returns)
export interface Account {
  login: number;
  server: string;
  currency: string;
  balance: number;
  equity: number;
  margin: number;
  margin_free: number;
  margin_level: number | null;
  leverage: number;
  kind: "DEMO" | "REAL" | "CONTEST" | "UNKNOWN";
  company: string;
}

export interface PositionRow {
  ticket: number;
  symbol: string;
  side: "BUY" | "SELL";
  volume: number;
  price_open: number;
  price_current: number;
  sl: number;
  tp: number;
  profit: number;
  swap: number;
  magic: number;
  comment: string;
  agent: boolean;
  time: string;
}

export interface Check {
  name: string;
  passed: boolean;
  detail: string;
  mandatory: boolean;
}

export interface SignalRow {
  signal_id: string;
  strategy_id: string;
  strategy_version: string;
  symbol: string;
  side: "BUY" | "SELL";
  timeframe: string;
  bar_time: string;
  entry: number;
  stop_loss: number;
  take_profit: number;
  rr: number;
  status: string;
  note: string;
  checks: Check[];
  ai: { score: number; approve: boolean; regime: string; reasons_for: string[]; reasons_against: string[]; model_version: string };
  ai_score: number | null;
  risk: { approved: boolean; checks: Check[]; volume: number; risk_money: number; risk_pct: number } | null;
  volume: number | null;
  risk_money: number | null;
  risk_pct: number | null;
  mode: string;
  created_at: string;
  outcome: { score: number; outcome: number | null; r_multiple: number | null } | null;
}

export interface TradeRow {
  id: number;
  signal_id: string;
  symbol: string;
  side: "BUY" | "SELL";
  status: string;
  requested_volume: number;
  filled_volume: number;
  fill_price: number | null;
  exit_price: number | null;
  sl: number;
  tp: number;
  realized_pnl: number | null;
  unrealized_pnl: number | null;
  commission: number;
  swap: number;
  opened_at: string | null;
  closed_at: string | null;
  close_reason: string;
  message: string;
  retcode: number | null;
  account_kind: string;
  source: string;
  strategy_version: string;
  created_at: string;
}

export interface EventRow {
  id?: number;
  time: string;
  level: string;
  category: string;
  message: string;
}

export interface Status {
  runner_alive: boolean;
  heartbeat_at: string | null;
  connected: boolean;
  reconciled: boolean;
  gateway: string;
  is_mock: boolean;
  mode: "ANALYSIS" | "DEMO" | "LIVE";
  state: "STOPPED" | "RUNNING" | "PAUSED" | "EMERGENCY_STOP";
  kill_switch: boolean;
  blocked_reasons: string[];
  account: Partial<Account>;
  day: { start_equity?: number | null; pnl?: number | null; loss_pct?: number | null; locked?: boolean };
  floating_drawdown_pct: number | null;
  positions: PositionRow[];
  orders: { ticket: number; symbol: string; type: number; volume: number; price: number; agent: boolean }[];
  last_error: string;
  allow_live_env: boolean;
  live_confirmed_at: string | null;
  strategy: { id: string; version: string; validation_status: string } | null;
  recent_signals: Pick<SignalRow, "signal_id" | "symbol" | "side" | "status" | "entry" | "stop_loss" | "take_profit" | "created_at" | "ai_score">[];
  recent_trades: TradeRow[];
  events: EventRow[];
}

export interface Metrics {
  trades: number;
  wins: number;
  losses: number;
  win_rate: number;
  gross_profit: number;
  gross_loss: number;
  net_profit: number;
  profit_factor: number | null;
  avg_win: number;
  avg_loss: number;
  expectancy: number;
  expectancy_r: number | null;
  max_drawdown: number;
  max_drawdown_pct: number | null;
  total_costs: number;
}
