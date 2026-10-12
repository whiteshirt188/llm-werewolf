import type { ChatMessage } from './chat.ts';

export const typingSpeeds = { slow: 260, normal: 180, fast: 100 } as const;
export type TypingSpeed = keyof typeof typingSpeeds;
export interface TypingState {
  seen: Set<string>;
  queue: { key: string; text: string; count: number }[];
}
export function initialTyping(messages: ChatMessage[]): TypingState {
  return { seen: new Set(messages.map(m => m.key)), queue: [] };
}
export function syncTyping(state: TypingState, all: ChatMessage[], visible: ChatMessage[], human: number, instant: boolean): TypingState {
  const visibleKeys = new Set(visible.map(m => m.key));
  const added = all.filter(m => !state.seen.has(m.key) && visibleKeys.has(m.key) && m.seat && m.seat !== human);
  return {
    seen: new Set([...state.seen, ...all.map(m => m.key)]),
    queue: instant ? [] : [...state.queue.filter(m => visibleKeys.has(m.key)),
      ...added.map(m => ({ key: m.key, text: m.text, count: 0 }))],
  };
}
export function tickTyping(state: TypingState): TypingState {
  const first = state.queue[0];
  if (!first) return state;
  const count = first.count + 1;
  return { ...state, queue: count >= Array.from(first.text).length ? state.queue.slice(1) :
    [{ ...first, count }, ...state.queue.slice(1)] };
}
export function displayedText(state: TypingState, key: string, text: string): string {
  const pending = state.queue.find(m => m.key === key);
  return pending ? Array.from(pending.text).slice(0, pending.count).join('') : text;
}
export function bubbleReady(state: TypingState, key: string): boolean {
  return !state.queue.slice(1).some(m => m.key === key);
}
