export function money(v: number | null | undefined, currency = ""): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "–";
  const s = v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return currency ? `${s} ${currency}` : s;
}

export function signed(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "–";
  const s = Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
  return `${v > 0 ? "+" : v < 0 ? "−" : ""}${s}`;
}

export function price(v: number | null | undefined): string {
  if (v === null || v === undefined || v === 0) return "–";
  const digits = v >= 1000 ? 2 : v >= 50 ? 3 : 5;
  return v.toFixed(digits);
}

export function pct(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "–";
  return `${v.toFixed(digits)}%`;
}

export function time(v: string | null | undefined): string {
  if (!v) return "–";
  const d = new Date(v);
  return d.toLocaleString("en-GB", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function ago(v: string | null | undefined): string {
  if (!v) return "never";
  const s = Math.round((Date.now() - new Date(v).getTime()) / 1000);
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  return `${Math.round(s / 3600)}h ago`;
}

export function tone(v: number | null | undefined): string {
  if (!v) return "";
  return v > 0 ? "pos" : "neg";
}
