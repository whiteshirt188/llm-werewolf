import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError, request } from './api.ts';
import type { GameView, Mutation } from './types.ts';

function rememberedGame(): string {
  const fromUrl = new URLSearchParams(location.search).get('game');
  if (fromUrl && /^[a-f0-9]{32}$/.test(fromUrl)) return fromUrl;
  try { return localStorage.getItem('werewolf.game') ?? ''; } catch { return ''; }
}
export function useGame() {
  const [id, setId] = useState(rememberedGame);
  const [game, setGame] = useState<GameView | null>(null);
  const [readError, setReadError] = useState('');
  const [unauthorized, setUnauthorized] = useState(false);
  const [connected, setConnected] = useState(false);
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const [pending, setPending] = useState<Mutation | null>(null);
  const [notice, setNotice] = useState('');
  const refreshRef = useRef<() => Promise<void>>(async () => {});
  const setActive = useCallback((next: string) => {
    setId(next); setPending(null); setNotice('');
    try { if (next) localStorage.setItem('werewolf.game', next); else localStorage.removeItem('werewolf.game'); }
    catch { /* The URL also keeps the non-secret game ID for reloads. */ }
    const url = new URL(location.href);
    if (next) url.searchParams.set('game', next); else url.searchParams.delete('game');
    history.replaceState(null, '', url);
  }, []);
  useEffect(() => {
    setGame(null); setReadError(''); setUnauthorized(false); setConnected(false);
    if (!id) { refreshRef.current = async () => {}; return; }
    let cancelled = false, inFlight = false, again = false;
    let source: EventSource | null = null;
    const sync = async () => {
      if (cancelled) return;
      if (inFlight) { again = true; return; }
      inFlight = true;
      try {
        const current = await request<GameView>(`/api/games/${id}`);
        if (!cancelled) {
          setGame(current); setReadError(''); setUnauthorized(false); setConnected(true);
          if (['finished','abandoned'].includes(current.status)) source?.close();
        }
      } catch (error) {
        if (!cancelled) {
          setReadError(error instanceof Error ? error.message : '读取局面失败。');
          setUnauthorized(error instanceof ApiError && error.status === 403); setConnected(false);
          if (error instanceof ApiError && [403, 404].includes(error.status)) source?.close();
        }
      } finally {
        inFlight = false;
        if (again && !cancelled) { again = false; void sync(); }
      }
    };
    refreshRef.current = sync;
    void sync();
    // Notifications only trigger a fresh GET: old SSE packets and old receipts cannot rewind the UI.
    source = new EventSource(`/api/games/${id}/stream`);
    source.addEventListener('state', () => { void sync(); });
    source.onopen = () => { void sync(); };
    const timer = setInterval(() => { void sync(); }, 2000);
    return () => { cancelled = true; source.close(); clearInterval(timer); };
  }, [id]);
  const mutate = async (mutation: Mutation): Promise<boolean> => {
    if (busyRef.current) return false;
    busyRef.current = true; setBusy(true); setNotice('');
    try {
      await request<GameView>(mutation.path, mutation.body);
      setPending(null);
      await refreshRef.current();
      return true;
    } catch (error) {
      setNotice(error instanceof Error ? error.message : '操作失败。');
      if (!(error instanceof ApiError) || error.status >= 500) setPending(mutation);
      else { setPending(null); await refreshRef.current(); }
      return false;
    } finally { busyRef.current = false; setBusy(false); }
  };
  return { id, game, setActive, readError, unauthorized, connected, busy, pending, notice, setNotice,
    mutate, refresh: () => refreshRef.current() };
}
