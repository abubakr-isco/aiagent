import { useCallback, useEffect, useRef, useState } from "react";

import { api } from "./api";

/** Poll an endpoint. Keeps the last good data visible when a refresh fails. */
export function usePoll<T>(path: string | null, intervalMs = 4000) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null);
  const alive = useRef(true);

  const load = useCallback(async () => {
    if (!path) return;
    try {
      const result = await api<T>(path);
      if (!alive.current) return;
      setData(result);
      setError(null);
      setUpdatedAt(new Date());
    } catch (e) {
      if (alive.current) setError((e as Error).message);
    }
  }, [path]);

  useEffect(() => {
    alive.current = true;
    load();
    if (!intervalMs) return () => void (alive.current = false);
    const id = window.setInterval(load, intervalMs);
    return () => {
      alive.current = false;
      window.clearInterval(id);
    };
  }, [load, intervalMs]);

  return { data, error, updatedAt, reload: load };
}
