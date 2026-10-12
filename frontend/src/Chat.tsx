import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { chatMessages, nearLatest, votingNoticeReady } from './chat.ts';
import type { GameView } from './types.ts';
import { Avatar } from './Avatar.tsx';
import { bubbleReady, displayedText, initialTyping, syncTyping, tickTyping, typingSpeeds } from './typing.ts';
import type { TypingSpeed } from './typing.ts';
import { PhaseNotice } from './PhaseNotice.tsx';

export function Chat({ game, hideSecrets, onTypingChange, onSpeechIntroChange }: { game: GameView; hideSecrets: boolean; onTypingChange?: (busy: boolean) => void; onSpeechIntroChange?: (pending: boolean) => void }) {
  const messages = chatMessages(game, hideSecrets);
  const signature = messages.map(m => m.key).join('|');
  const all = chatMessages(game);
  const allSignature = all.map(m => m.key).join('|');
  const [typing, setTyping] = useState(() => initialTyping(all));
  useLayoutEffect(() => { onTypingChange?.(typing.queue.length > 0); }, [typing.queue.length, onTypingChange]);
  const [speed, setSpeed] = useState<TypingSpeed>('normal');
  const [reduced, setReduced] = useState(() => window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  useEffect(() => {
    const preference = window.matchMedia('(prefers-reduced-motion: reduce)');
    const change = () => setReduced(preference.matches);
    preference.addEventListener('change', change);
    return () => preference.removeEventListener('change', change);
  }, []);
  useLayoutEffect(() => {
    setTyping(previous => syncTyping(previous, all, messages, game.human_seat, reduced || game.status === 'finished'));
  }, [allSignature, signature, reduced, game.status, game.human_seat]);
  useEffect(() => {
    if (!typing.queue.length || game.status === 'paused' || reduced) return;
    const timer = window.setTimeout(() => setTyping(tickTyping), typingSpeeds[speed]);
    return () => window.clearTimeout(timer);
  }, [typing, speed, game.status, reduced]);
  const viewport = useRef<HTMLDivElement>(null);
  const follow = useRef(true);
  const known = useRef(new Set<string>());
  const [unread, setUnread] = useState<string[]>([]);
  const [atLatest, setAtLatest] = useState(true);
  const latest = () => {
    if (viewport.current) viewport.current.scrollTop = viewport.current.scrollHeight;
    follow.current = true; setUnread([]); setAtLatest(true);
  };
  useLayoutEffect(() => {
    const keys = signature ? signature.split('|') : [];
    const current = new Set(keys);
    const added = keys.filter(key => !known.current.has(key));
    known.current = new Set([...known.current, ...current]);
    if (follow.current) latest();
    else setUnread(previous => [...previous.filter(key => current.has(key)), ...added]);
  }, [signature]);
  useLayoutEffect(() => { if (follow.current) latest(); }, [typing]);
  useLayoutEffect(() => {
    const element = viewport.current;
    if (!element) return;
    const observer = new ResizeObserver(() => { if (follow.current) latest(); });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  const nickname = (seat: number) => seat === game.human_seat ? '你' :
    game.seat_profiles.find(p => p.seat === seat)?.nickname || `玩家 ${seat}`;
  return <section className="chat-panel" aria-label="对话">
    <div className="chat-heading"><h2>对话</h2><div className="typing-controls"><label>吐字速度<select aria-label="吐字速度" value={speed} onChange={e => setSpeed(e.target.value as TypingSpeed)} disabled={reduced}>
      <option value="slow">慢</option><option value="normal">正常</option><option value="fast">快</option>
    </select></label>{typing.queue.length > 0 && <button className="text-button" onClick={() => setTyping(previous => ({ ...previous, queue: [] }))}>显示全文</button>}</div><span className="muted">第 {game.view.day} 天 · {game.view.alive ? '你的可见消息' : '公开观战'}</span></div>
    <PhaseNotice game={game} reduced={reduced} pendingKeys={typing.queue.map(message => message.key)} onSpeechIntroChange={onSpeechIntroChange}>
    <div className="chat-messages" ref={viewport} tabIndex={0} aria-label="对话消息，可滚动查看历史"
      onScroll={() => {
        const el = viewport.current;
        if (!el) return;
        follow.current = nearLatest(el.scrollTop, el.clientHeight, el.scrollHeight);
        setAtLatest(follow.current);
        if (follow.current) setUnread([]);
      }}>
      {messages.length === 0 && <p className="empty">静候天亮，公开发言会出现在这里。</p>}
      {messages.map((message, index) => {
        if (!votingNoticeReady(game, messages, index, typing.queue.map(m => m.key))) return null;
        if (!bubbleReady(typing, message.key)) return null;
        const mine = message.seat === game.human_seat;
        const batchStart = message.batch && (index === 0 ||
          messages[index - 1].key.substring(0, messages[index - 1].key.lastIndexOf(':')) !==
          message.key.substring(0, message.key.lastIndexOf(':')));
        return <div key={message.key}>
          {batchStart && <p className="batch-marker">第 {message.day} 天 · 本轮{message.batch}统一公布 · 仅狼队可见</p>}
          {message.seat ? <article className={`chat-row ${mine ? 'self' : ''} ${message.scope === 'wolves' ? 'wolf-chat' : ''}`}>
            <Avatar seat={message.seat} />
            <div className="chat-content"><div className="chat-meta"><b>{nickname(message.seat)}</b><span>{message.seat} 号 · 第 {message.day} 天{message.kind === 'last_words' ? ' · 遗言' : ''}</span></div>
              <div className="chat-bubble">{message.scope === 'wolves' && <span className="message-scope">仅狼队可见 · {message.title}</span>}<p>{displayedText(typing, message.key, message.text)}{typing.queue[0]?.key === message.key && <span className="typing-cursor" aria-hidden="true">▍</span>}</p>
                {typing.queue.some(m => m.key === message.key) && <button className="text-button bubble-expand" onClick={() => setTyping(previous => ({ ...previous, queue: previous.queue.filter(m => m.key !== message.key) }))}>显示这条全文</button>}
              </div>
            </div>
          </article> : <article className={`chat-system ${message.scope === 'wolves' ? 'wolf-chat' : ''}`}>
            <b>{message.title}</b><span>第 {message.day} 天{message.scope === 'wolves' ? ' · 仅狼队可见' : ''}</span>
            {message.expandable ? <details><summary>展开票型</summary><p>{message.text}</p></details> : <p>{message.text}</p>}
          </article>}
        </div>;
      })}
    </div>
    </PhaseNotice>
    {!atLatest && <button className="latest-button secondary" onClick={latest}>{unread.length ? `${unread.length} 条新消息 · ` : ''}回到最新 ↓</button>}
  </section>;
}
