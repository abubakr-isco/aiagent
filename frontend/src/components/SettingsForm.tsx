import { useEffect, useState } from "react";

import { ErrorNote } from "./ui";

export interface FieldSpec {
  key: string;
  label: string;
  hint?: string;
  type: "number" | "boolean" | "text" | "select";
  options?: string[];
  step?: number;
}

/** Generic form for flat settings objects. Read-only unless `editable`. */
export function SettingsForm({
  fields, values, editable, onSave, saveLabel = "Save changes", warning,
}: {
  fields: FieldSpec[];
  values: Record<string, unknown>;
  editable: boolean;
  onSave: (changed: Record<string, unknown>) => Promise<void>;
  saveLabel?: string;
  warning?: string;
}) {
  const [draft, setDraft] = useState<Record<string, unknown>>(values);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => setDraft(values), [values]);

  const changed = Object.fromEntries(fields.filter((f) => draft[f.key] !== values[f.key]).map((f) => [f.key, draft[f.key]]));
  const dirty = Object.keys(changed).length > 0;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    setSaved(false);
    try {
      await onSave(changed);
      setSaved(true);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="settings" onSubmit={submit}>
      <div className="settings-grid">
        {fields.map((f) => (
          <label key={f.key} className={`field ${f.type === "boolean" ? "field-check" : ""}`}>
            <span>{f.label}</span>
            {f.type === "boolean" ? (
              <input type="checkbox" checked={Boolean(draft[f.key])} disabled={!editable} onChange={(e) => setDraft({ ...draft, [f.key]: e.target.checked })} />
            ) : f.type === "select" ? (
              <select value={String(draft[f.key] ?? "")} disabled={!editable} onChange={(e) => setDraft({ ...draft, [f.key]: e.target.value })}>
                {f.options?.map((o) => <option key={o} value={o}>{o}</option>)}
              </select>
            ) : (
              <input
                type={f.type}
                step={f.step ?? "any"}
                value={draft[f.key] === null || draft[f.key] === undefined ? "" : String(draft[f.key])}
                disabled={!editable}
                onChange={(e) => setDraft({ ...draft, [f.key]: f.type === "number" ? (e.target.value === "" ? null : Number(e.target.value)) : e.target.value })}
              />
            )}
            {f.hint && <small>{f.hint}</small>}
          </label>
        ))}
      </div>
      {editable && (
        <div className="settings-actions">
          {dirty && warning && <p className="caution">{warning}</p>}
          <ErrorNote error={error} />
          {saved && !dirty && <p className="muted">Saved. The agent applies new settings on its next cycle.</p>}
          <button className="btn btn-primary" disabled={!dirty || busy}>{busy ? "Saving…" : saveLabel}</button>
          {dirty && <button type="button" className="btn" onClick={() => setDraft(values)}>Discard</button>}
        </div>
      )}
      {!editable && <p className="muted small">Only an admin can change these values.</p>}
    </form>
  );
}
