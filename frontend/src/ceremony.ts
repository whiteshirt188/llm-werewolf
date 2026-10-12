import { chatMessages, votingNoticeReady } from './chat.ts';
import { isPrivate } from './game.ts';
import type { GameView } from './types.ts';

export interface Ceremony {
  key: string; day: number;
  kind: 'night' | 'dawn' | 'speech' | 'vote' | 'result' | 'tie' | 'victory';
  title: string; detail: string;
  winner?: 'good' | 'wolves';
}

// Only public facts may drive a public ceremony; never inspect private progress.
export function ceremonies(game: GameView, pendingKeys: string[] = []): Ceremony[] {
  if (game.preparation?.active) return [];
  const messages = chatMessages(game);
  const speechDays = new Set<number>();
  const publicEvents = game.view.events.filter(event => !isPrivate(event));
  let publicIndex = 0;
  return publicEvents.flatMap((event, index): Ceremony[] => {
    const base = { key: `${game.game_id}:ceremony:${++publicIndex}:${event.day}:${event.kind}`, day: event.day };
    const d = event.data;
    switch (event.kind) {
      case 'night_started': return [{ ...base, kind: 'night', title: '天黑请闭眼', detail: '夜幕降临，等待天亮揭晓。' }];
      case 'dawn': {
        const dawn: Ceremony = { ...base, kind: 'dawn', title: '天亮了', detail: Array.isArray(d.dead) && d.dead.length
          ? `${d.dead.map(seat => `${seat} 号`).join('、')} 昨夜出局，身份不公开。` : '昨夜无人出局。' };
        // Daytime starts at the public dawn, not after the first player submits.
        // A game decided at dawn emits game_over immediately, with no speech phase.
        if (['game_over','hunter_ready','last_words_started'].includes(publicEvents[index + 1]?.kind ?? '') || speechDays.has(event.day)) return [dawn];
        speechDays.add(event.day);
        return [dawn, { ...base, key: `${base.key}:speech-start`, kind: 'speech',
          title: '白天发言开始', detail: '听完每个人的判断，再作决定。' }];
      }
      case 'day_started':
      case 'speak':
        if (speechDays.has(event.day)) return [];
        speechDays.add(event.day);
        return [{ ...base, kind: 'speech', title: '白天发言开始', detail: '听完每个人的判断，再作决定。' }];
      case 'voting_started': {
        const index = messages.findIndex(message => message.kind === 'voting_started' && message.day === event.day
          && message.title === (d.round === 2 ? '开始复投' : '开始投票'));
        if (index >= 0 && !votingNoticeReady(game, messages, index, pendingKeys)) return [];
        return [{ ...base, kind: 'vote', title: d.round === 2 ? '复投开始' : '放逐投票开始', detail: '选票全部提交后统一公开。' }];
      }
      case 'vote_result': return [{ ...base, kind: 'result', title: '票型揭晓', detail: `第 ${d.round} 轮投票已结算，完整票型见对话。` }];
      case 'hunter_ready': return [{...base,kind:'result',title:'猎人举枪',detail:`${d.seat} 号还有最后一次选择，开枪完成后判定胜负。`}];
      case 'last_words_started': return [{...base,kind:'result',title:'最后的发言',detail:'请听完遗言，再进入下一个节点。遗言中的身份声明不自动成为事实。'}];
      case 'hunter_shot': return [{...base,kind:'result',title:d.target == null ? '猎人放下枪' : '枪声响起',detail:d.target == null ? '猎人放弃开枪。' : `${d.target} 号被猎人带走。`}];
      case 'knight_duel': return [{...base,kind:'result',title:d.success ? '骑士决斗成功' : '骑士决斗失败',detail:d.success ? `${d.target} 号狼人出局，夜幕即将降临。` : `${d.seat} 号骑士出局，白天继续。`}];
      case 'eliminated': return [{ ...base, kind: 'result', title: `${d.seat} 号被放逐`, detail: '身份暂不公开，请留意接下来的局面。' }];
      case 'no_elimination': return [{ ...base, kind: 'result', title: '本轮无人出局', detail: d.reason === 'tie' ? '复投仍平票，夜幕即将降临。' : '没有有效票，夜幕即将降临。' }];
      case 'runoff_started': return [{ ...base, kind: 'tie', title: '出现平票 · 进入辩论', detail: Array.isArray(d.candidates)
        ? `${d.candidates.map(seat => `${seat} 号`).join('、')} 进入辩论，随后复投。` : '候选玩家辩论后复投。' }];
      case 'game_over': return [{ ...base, kind: 'victory', winner: d.winner === 'good' ? 'good' : 'wolves',
        title: d.winner === 'good' ? '好人阵营获胜' : '狼人阵营获胜', detail: '身份已揭晓，完整对局可以复盘。' }];
      default: return [];
    }
  });
}

export interface CeremonyState { seen: Set<string>; queue: Ceremony[] }
export function speechIntroPending(state: CeremonyState, game: GameView): boolean {
  return game.status === 'running' && game.view.phase === 'day_speech'
    && state.queue.some(cue => cue.kind === 'speech' && cue.day === game.view.day);
}
export function ceremonyTiming(cue: Ceremony) {
  const enterMs = 650;
  const holdMs = cue.kind === 'victory' ? 2800 : 2000;
  const exitMs = cue.kind === 'victory' ? 1050 : 850;
  return { enterMs, holdMs, exitMs, exitDelayMs: enterMs + holdMs, totalMs: enterMs + holdMs + exitMs };
}
export function finishCeremony(state: CeremonyState, key: string): CeremonyState {
  // An old timeout must never dismiss a newly started or manually skipped-to cue.
  return state.queue[0]?.key === key ? { ...state, queue: state.queue.slice(1) } : state;
}
export function initialCeremonies(cues: Ceremony[]): CeremonyState {
  return { seen: new Set(cues.map(cue => cue.key)), queue: [] };
}
export function syncCeremonies(state: CeremonyState, cues: Ceremony[], suppressed: boolean): CeremonyState {
  const seen = new Set(state.seen);
  const added = cues.filter(cue => !seen.has(cue.key));
  for (const cue of cues) seen.add(cue.key);
  const current = new Set(cues.map(cue => cue.key));
  // A newer visible step supersedes an unfinished older ceremony batch.
  return { seen, queue: suppressed ? [] : added.length ? added : state.queue.filter(cue => current.has(cue.key)) };
}
