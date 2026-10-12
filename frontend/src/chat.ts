import { describeEvent, isPrivate } from './game.ts';
import type { GameView } from './types.ts';

export interface ChatMessage {
  key: string;
  day: number;
  seat?: number;
  scope: 'public' | 'wolves';
  title: string;
  text: string;
  batch?: '提案' | '回应';
  expandable?: boolean;
  kind?: string;
}

const wolfKinds = new Set(['wolf_proposals', 'wolf_replies', 'wolf_decision', 'wolf_message']);

// This presentation filter is defensive; API views must already enforce permissions.
export function chatMessages(game: GameView, hideSecrets = false): ChatMessage[] {
  const canSeeWolves = game.view.alive && game.view.role === 'wolf' && !hideSecrets;
  let publicIndex = 0, wolfIndex = 0;
  return game.view.events.flatMap(event => {
    const privateEvent = isPrivate(event);
    if (privateEvent && (!canSeeWolves || !wolfKinds.has(event.kind))) return [];
    if (event.audience && !event.audience.includes(game.human_seat)) return [];
    const base = {
      kind: event.kind,
      key: `${game.game_id}:${privateEvent ? 'wolves' : 'public'}:${privateEvent ? ++wolfIndex : ++publicIndex}:${event.day}:${event.kind}`,
      day: event.day, scope: privateEvent ? 'wolves' as const : 'public' as const,
    };
    if (event.kind === 'wolf_proposals' || event.kind === 'wolf_replies') {
      const proposals = event.kind === 'wolf_proposals';
      const entries = Object.entries(event.data[proposals ? 'proposals' : 'replies'] as Record<string, unknown>)
        .sort(([a], [b]) => Number(a) - Number(b));
      return entries.map(([seat, value]): ChatMessage => {
        const proposal = value as { target: number; text: string };
        return { ...base, key: `${base.key}:${seat}`, seat: Number(seat),
          title: proposals ? '刀人提案' : '协商回应', batch: proposals ? '提案' : '回应',
          text: proposals ? `提议刀 ${proposal.target} 号：${proposal.text}` : String(value) };
      });
    }
    const item = describeEvent(event);
    return [{ ...base, ...item,
      seat: ['speak', 'wolf_message', 'last_words'].includes(event.kind) ? Number(event.data.seat) : undefined,
      expandable: event.kind === 'vote_result' }];
  });
}

// Engine state stays authoritative; only the presentation waits for the final speech.
export function presentationPhase(game: GameView, typingBusy = false): string {
  if (game.preparation?.active && !['finished', 'abandoned'].includes(game.status)) return 'preparation';
  const phase = game.view.phase;
  // A private, optional skill decision must never become a public role announcement.
  if (phase === 'knight_duel') return 'day_speech';
  const finalSpeechGate = (game.step_waiting || game.reading) && game.speech_to_vote !== false;
  if ((phase === 'vote' || phase === 'runoff_vote') && (finalSpeechGate || typingBusy)) {
    return phase === 'vote' ? 'day_speech' : 'runoff_speech';
  }
  return phase;
}

export function votingNoticeReady(game: GameView, messages: ChatMessage[], index: number,
  pendingKeys: string[]): boolean {
  const message = messages[index];
  if (message.kind !== 'voting_started') return true;
  const latestVoting = messages.length - 1 - [...messages].reverse().findIndex(m => m.kind === 'voting_started');
  // Do not hide old historical rounds when today's last speaker is still typing.
  if (index !== latestVoting || message.day !== game.view.day) return true;
  if (!['vote', 'runoff_vote'].includes(game.view.phase)) return true;
  return presentationPhase(game, messages.slice(0, index).some(m => pendingKeys.includes(m.key))) === game.view.phase;
}

export function nearLatest(scrollTop: number, clientHeight: number, scrollHeight: number): boolean {
  return scrollHeight - clientHeight - scrollTop <= 48;
}

export function waitingText(game: GameView, typingBusy = false): string {
  if (game.status === 'abandoned') return '本局已结束，未判胜负；存档与用量保留，不再调用模型。';
  if (game.status === 'finished') return '对局结束，可以查看赛后复盘。';
  if (game.status === 'paused') return game.decision_recovery ? '上次行动被截断，请选择下方的恢复方式。' : '对局已完全暂停，点击“恢复运行”回到原模式。';
  if (game.preparation?.active) return `玩家正在拿牌准备（${game.preparation.completed}/${game.preparation.total}），你可以先查看自己的身份。`;
  if (game.step_waiting) return '本步展示完后，点击“继续下一步”。后台最多准备下一步，不提前结算。';
  if (game.reading) return '正在发言与阅读，窗口结束后再推进下一步。';
  if (typingBusy && ['vote', 'runoff_vote'].includes(game.view.phase)) return '等最后的发言完整展示后，再进入投票。';
  if (game.turn_version && game.view.legal_actions[0]?.kind === 'last_words') return '轮到你发表遗言；可查看旧查验，写完提交或留空跳过。';
  if (!game.view.alive && !['shoot','last_words'].includes(game.view.legal_actions[0]?.kind ?? '')) return '你已出局，正在公开观战。';
  if (game.turn_version) return '轮到你了，请完成下方行动。';
  if (game.view.phase === 'night') return '夜间行动进行中，请等待。';
  if (game.view.current_actor) return `等待 ${game.view.current_actor} 号行动。`;
  return '等待其他玩家行动。';
}
