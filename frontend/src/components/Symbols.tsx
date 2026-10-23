import { useState } from "react";

import { api, post, type Paged } from "../api";
import { usePoll } from "../hooks";
import { Empty, ErrorNote } from "./ui";

interface SymbolRow { id: number; name: string; enabled: boolean; max_spread_points: number | null; notes: string }

/** Broker symbol names differ (EURUSD, EURUSD.m, EURUSDpro). The agent trades exactly these names. */
export function Symbols({ editable }: { editable: boolean }) {
  const { data, error, reload } = usePoll<Paged<SymbolRow>>("/market/symbols/", 0);
  const [name, setName] = useState("");
  const [actionError, setActionError] = useState<string | null>(null);

  async function run(fn: () => Promise<unknown>) {
    setActionError(null);
    try {
      await fn();
      await reload();
    } catch (e) {
      setActionError((e as Error).message);
    }
  }

  return (
    <>
      <ErrorNote error={error ?? actionError} />
      {data?.results.length === 0 && <Empty>No symbols configured. Add the exact names your broker uses.</Empty>}
      {data && data.results.length > 0 && (
        <table className="table table-compact">
          <thead><tr><th>Symbol</th><th>Analysed and traded</th><th className="num">Spread cap, points</th>{editable && <th />}</tr></thead>
          <tbody>
            {data.results.map((s) => (
              <tr key={s.id}>
                <td>{s.name}</td>
                <td>
                  <input type="checkbox" checked={s.enabled} disabled={!editable} aria-label={`Enable ${s.name}`}
                    onChange={(e) => run(() => api(`/market/symbols/${s.id}/`, { method: "PATCH", json: { enabled: e.target.checked } }))} />
                </td>
                <td className="num">
                  <input type="number" min={1} defaultValue={s.max_spread_points ?? ""} disabled={!editable} placeholder="none" aria-label={`Spread cap for ${s.name}`} style={{ width: 90 }}
                    onBlur={(e) => run(() => api(`/market/symbols/${s.id}/`, { method: "PATCH", json: { max_spread_points: e.target.value ? Number(e.target.value) : null } }))} />
                </td>
                {editable && <td><button className="link" onClick={() => run(() => api(`/market/symbols/${s.id}/`, { method: "DELETE" }))}>Remove</button></td>}
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {editable && (
        <form className="filters" style={{ marginTop: 12 }} onSubmit={(e) => { e.preventDefault(); if (name) run(() => post("/market/symbols/", { name, enabled: true })).then(() => setName("")); }}>
          <input value={name} onChange={(e) => setName(e.target.value.trim())} placeholder="e.g. EURUSD.m" aria-label="New symbol name" />
          <button className="btn" disabled={!name}>Add symbol</button>
        </form>
      )}
    </>
  );
}
