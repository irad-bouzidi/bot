import { useCallback, useEffect, useRef, useState } from 'react';
import { getPreferences, savePreferences } from './api';

// Every input on the dashboard that is not a trading parameter -- theme, active
// view, the backtest form's values -- is stored here, in Postgres, through
// /preferences. It replaces localStorage.
//
// Two things this has to get right:
//
//   * **Never write defaults over stored values.** The load is async, so for a
//     moment the app holds its own defaults. If a write fired in that window
//     it would overwrite a stored theme with the default one. `ready` gates
//     every write until the first read has returned.
//   * **Never lose a stored value because one poll failed.** A failed read
//     leaves `available` false and the app keeps its last known state rather
//     than resetting; a failed write is reported and the local value is kept,
//     because throwing away what the user just typed is worse than a stale row.
//
// Writes are debounced. A number input fires per keystroke, and one round trip
// per digit would put ~8 rows a second into a table nobody is reading that fast.
const WRITE_DEBOUNCE_MS = 400;

export interface PreferencesState {
  prefs: Record<string, any>;
  /** The first read has completed (successfully or not). Gates every write. */
  ready: boolean;
  /** The store answered. False means preferences are not being persisted. */
  available: boolean;
  error: string | null;
  /** Shallow-merge a patch: local immediately, server after the debounce. */
  update: (patch: Record<string, any>) => void;
  /** Read one key with a fallback, before or after the load completes. */
  get: <T,>(key: string, fallback: T) => T;
}

export function usePreferences(): PreferencesState {
  const [prefs, setPrefs] = useState<Record<string, any>>({});
  const [ready, setReady] = useState(false);
  const [available, setAvailable] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Accumulates the fields changed since the last flush, so a debounced write
  // sends everything that was edited during the window rather than only the
  // final keystroke's field.
  const pending = useRef<Record<string, any>>({});
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await getPreferences();
        if (cancelled) return;
        setPrefs(res.preferences || {});
        setAvailable(res.available);
        setError(res.available ? null : res.error || 'Preferences are not being saved.');
      } catch (e: any) {
        if (cancelled) return;
        setAvailable(false);
        setError(e?.message || 'Could not load saved preferences.');
      } finally {
        if (!cancelled) setReady(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const flush = useCallback(async () => {
    const patch = pending.current;
    pending.current = {};
    if (!Object.keys(patch).length) return;
    try {
      const res = await savePreferences(patch);
      setAvailable(res.available);
      setError(res.available ? null : res.error || 'Preferences are not being saved.');
      // Deliberately does NOT adopt the server's document over local state. A
      // reply that arrives after a later keystroke would rewrite the digits
      // under the cursor -- the same reason the sizing form ignores polls
      // while dirty.
    } catch (e: any) {
      setAvailable(false);
      setError(e?.message || 'Could not save preferences.');
    }
  }, []);

  const update = useCallback(
    (patch: Record<string, any>) => {
      setPrefs(prev => ({ ...prev, ...patch }));
      if (!ready) return; // never write defaults over what is stored
      pending.current = { ...pending.current, ...patch };
      if (timer.current) clearTimeout(timer.current);
      timer.current = setTimeout(flush, WRITE_DEBOUNCE_MS);
    },
    [flush, ready],
  );

  // Flush on unmount so a value typed just before a navigation is not lost in
  // the debounce window.
  useEffect(
    () => () => {
      if (timer.current) {
        clearTimeout(timer.current);
        void flush();
      }
    },
    [flush],
  );

  const get = useCallback(
    <T,>(key: string, fallback: T): T =>
      prefs[key] === undefined || prefs[key] === null ? fallback : (prefs[key] as T),
    [prefs],
  );

  return { prefs, ready, available, error, update, get };
}
