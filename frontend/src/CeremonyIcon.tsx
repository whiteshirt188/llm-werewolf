import type { Ceremony } from './ceremony.ts';

export function CeremonyIcon({ kind }: { kind: Ceremony['kind'] }) {
  const drawing = kind === 'night' ? <path d="M44 8A25 25 0 1 0 56 44 23 23 0 0 1 44 8Z" fill="currentColor" stroke="none" /> :
    kind === 'dawn' ? <><circle cx="32" cy="32" r="11" fill="currentColor" stroke="none" /><path d="M32 4v9m0 38v9M4 32h9m38 0h9M12 12l6 6m28 28 6 6M12 52l6-6m28-28 6-6" /></> :
    kind === 'speech' ? <><path d="M12 14h40v30H32L20 53v-9h-8Z" /><path d="M21 25h22M21 33h15" /></> :
    kind === 'vote' ? <><path d="m24 7 25 12-12 24-25-12Z" /><path d="m24 24 4 5 10-5M8 39l8 17h40l-6-17M22 44h19" /></> :
    kind === 'tie' ? <><path d="M9 21h44l-8-8M55 43H11l8 8M53 21l-8 8M11 43l8-8" /><path d="M29 29v7m6-7v7" /></> :
    kind === 'result' ? <><path d="M32 9v43M13 20h38M22 55h20M15 21 7 39h16ZM49 21l-8 18h16Z" /><circle cx="32" cy="15" r="3" fill="currentColor" /></> :
      <><path d="M20 10h24v17c0 12-24 12-24 0ZM20 15H9v8c0 8 6 11 13 11M44 15h11v8c0 8-6 11-13 11M32 38v12M21 55h22M26 50h12" /><path d="m32 17 2 5 5 1-4 3 1 5-4-3-4 3 1-5-4-3 5-1Z" fill="currentColor" stroke="none" /></>;
  return <svg viewBox="0 0 64 64" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{drawing}</svg>;
}
