import { useState } from "react";

import type { EventRow, Paged } from "../api";
import { useConsole } from "../components/Shell";
import { Empty, ErrorNote, Section } from "../components/ui";
import { time } from "../format";
import { usePoll } from "../hooks";

interface AuditRow { id: number; time: string; actor_name: string; action: string; target: string; before: unknown; after: unknown; ip: string | null }

export default function Logs() {
  const { can } = useConsole();
  const [level, setLevel] = useState("");
  const [category, setCategory] = useState("");
  const [search, setSearch] = useState("");
  const q = new URLSearchParams({ limit: "200", ...(level && { level }), ...(category && { category }), ...(search && { search }) });
  const events = usePoll<Paged<EventRow>>(`/events/?${q}`, 5000);
  const audit = usePoll<Paged<AuditRow>>(can("admin") ? "/audit/?limit=100" : null, 15000);

  return (
    <>
      <Section title="Agent events" aside={
        <div className="filters">
          <select value={level} onChange={(e) => setLevel(e.target.value)} aria-label="Level">
            <option value="">All levels</option><option value="WARNING,ERROR,CRITICAL">Warnings and errors</option><option value="ERROR,CRITICAL">Errors only</option><option value="CRITICAL">Critical</option>
          </select>
          <select value={category} onChange={(e) => setCategory(e.target.value)} aria-label="Category">
            <option value="">All categories</option>
            {["agent", "execution", "risk", "positions", "connection", "command"].map((c) => <option key={c}>{c}</option>)}
          </select>
          <input placeholder="Search" value={search} onChange={(e) => setSearch(e.target.value)} aria-label="Search messages" />
        </div>
      }>
        <ErrorNote error={events.error} />
        {events.data?.results.length === 0 && <Empty>No events match.</Empty>}
        <ol className="journal">
          {events.data?.results.map((e) => (
            <li key={e.id} className={`lvl-${e.level.toLowerCase()}`}>
              <time>{time(e.time)}</time><span className="cat">{e.category}</span><span>{e.message}</span>
            </li>
          ))}
        </ol>
      </Section>
      {can("admin") && (
        <Section title="Audit trail">
          <p className="muted">Every command, mode change and settings change, with who made it.</p>
          <div className="scroll-x">
            <table className="table table-compact">
              <thead><tr><th>Time</th><th>User</th><th>Action</th><th>Target</th><th>Change</th><th>IP</th></tr></thead>
              <tbody>
                {audit.data?.results.map((a) => (
                  <tr key={a.id}>
                    <td>{time(a.time)}</td><td>{a.actor_name}</td><td>{a.action}</td><td>{a.target}</td>
                    <td className="wrap small">{a.before ? `${JSON.stringify(a.before)} to ` : ""}{a.after ? JSON.stringify(a.after) : ""}</td><td>{a.ip ?? "–"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Section>
      )}
    </>
  );
}
