import { useEffect, useLayoutEffect, useState } from 'react';
import type { CSSProperties, ReactNode } from 'react';
import type { GameView } from './types.ts';
import { presentationPhase } from './chat.ts';
import { ceremonies, ceremonyTiming, finishCeremony, initialCeremonies, speechIntroPending, syncCeremonies } from './ceremony.ts';
import { CeremonyIcon } from './CeremonyIcon.tsx';

const steps = ['夜晚', '天亮 / 发言', '投票 / 结算', '结束'];
export function PhaseNotice({ game, reduced, pendingKeys, children, onSpeechIntroChange }: {
  game: GameView; reduced: boolean; pendingKeys: string[]; children: ReactNode; onSpeechIntroChange?: (pending: boolean) => void;
}) {
  const phase = presentationPhase(game, pendingKeys.length > 0);
  const step = phase === 'preparation' ? -1 : phase === 'night' ? 0 : phase === 'day_speech' || phase === 'runoff_speech' ? 1 : phase === 'finished' ? 3 : 2;
  const cues = ceremonies(game, pendingKeys);
  const signature = cues.map(cue => cue.key).join('|');
  const [state, setState] = useState(() => {
    const initial = initialCeremonies(cues);
    try {
      if (!sessionStorage.getItem(`werewolf:ceremony:${game.game_id}`) && !reduced && game.status === 'running'
        && game.view.events.every(event => ['game_started', 'night_started'].includes(event.kind))) {
        initial.queue = cues.filter(cue => cue.kind === 'night');
      }
    } catch { /* No storage: do not replay the opening. */ }
    return initial;
  });
  useEffect(() => {
    try { sessionStorage.setItem(`werewolf:ceremony:${game.game_id}`, 'seen'); } catch { /* Optional display marker. */ }
  }, [game.game_id]);
  useLayoutEffect(() => {
    setState(previous => syncCeremonies(previous, cues, game.status === 'paused' || reduced));
  }, [signature, game.status, reduced]);
  const active = state.queue[0];
  const introPending = !reduced && speechIntroPending(state, game);
  useLayoutEffect(() => { onSpeechIntroChange?.(introPending); }, [introPending, onSpeechIntroChange]);
  const timing = active ? ceremonyTiming(active) : null;
  const dismiss = () => { if (active) setState(previous => finishCeremony(previous, active.key)); };
  const title = phase === 'preparation' ? '拿牌准备 · 可以先看看自己的身份' : phase === 'night' ? '天黑请闭眼' : phase === 'day_speech' ? '白天 · 依次发言' :
    phase === 'last_words' ? '最后的发言 · 公开遗言' : phase === 'hunter_shot' ? '猎人 · 开枪决定' :
    phase === 'runoff_speech' ? '平票 · 开始辩论' : phase === 'runoff_vote' ? '开始复投' :
      phase === 'finished' ? game.view.winner === 'good' ? '好人阵营获胜' : '狼人阵营获胜' : '开始投票';
  useEffect(() => {
    if (!active) return;
    const timer = window.setTimeout(() => setState(previous => finishCeremony(previous, active.key)), ceremonyTiming(active).totalMs);
    return () => window.clearTimeout(timer);
  }, [active?.key]);
  return <div className="phase-stage"><div className={`phase-strip ${phase === 'night' ? 'night-strip' : ''}`}>
    <nav aria-label="公开对局流程">{steps.map((label, index) => <span key={label} aria-current={index === step ? 'step' : undefined}>{label}</span>)}</nav>
    <div className="phase-caption"><b>{title}</b></div>
    </div><div className="ceremony-viewport">{children}
      {active && timing && <div key={active.key} className={`ceremony-overlay ceremony-${active.kind}${active.winner ? ` ceremony-${active.winner}` : ''}`}
        style={{ '--ceremony-enter': `${timing.enterMs}ms`, '--ceremony-exit': `${timing.exitMs}ms`,
          '--ceremony-exit-delay': `${timing.exitDelayMs}ms`, '--ceremony-duration': `${timing.totalMs}ms` } as CSSProperties}>
        <div className="ceremony-backdrop" aria-hidden="true" /><div className="ceremony-light" aria-hidden="true" />
        <div className="ceremony-sparks" aria-hidden="true">{Array.from({ length: 7 }, (_, index) => <i key={index} style={{
          '--spark-x': `${14 + index * 12}%`, '--spark-delay': `${index * 110}ms`, '--spark-drift': `${(index % 3 - 1) * 24}px`,
        } as CSSProperties} />)}</div>
        <div className="ceremony" role="status" aria-live="polite">
          <div className="ceremony-orbit" aria-hidden="true"><i /><i /><i /><CeremonyIcon kind={active.kind} /></div>
          <div className="ceremony-copy"><span className="ceremony-kicker">第 {active.day} 天 · 局面播报</span><h3>{active.title}</h3><p>{active.detail}</p></div>
        </div>
        <button type="button" className="ceremony-skip" onClick={dismiss}>跳过动画{state.queue.length > 1 ? ` · 余 ${state.queue.length - 1}` : ''}</button>
      </div>}
    </div></div>;
}
