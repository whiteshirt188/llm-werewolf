import { useEffect, useRef, useState } from 'react';
import type { CSSProperties } from 'react';
import { Chat } from './Chat.tsx';
import { Avatar } from './Avatar.tsx';
import { AccountSettings } from './AccountSettings.tsx';
import type { Account } from './account.ts';
import { TablePlanner } from './TablePlanner.tsx';
import { RosterSettings } from './RosterSettings.tsx';
import { seatNumbers, sixRoster, rosterError, ruleConfig } from './roster.ts';
import type { Roster } from './roster.ts';
import { GameUsage } from './GameUsage.tsx';
import { presentationPhase, waitingText } from './chat.ts';
import { request } from './api.ts';
import { actions, countChars, defaultPersona, defaultPlayStyles, playStyleNames, describeEvent, isPrivate, makeAction, pauseReason,
  phases, presets, roles, potionNames, rosterText, wolfVictoryText, wolfObjective } from './game.ts';
import type { Action, ActionSpec, GameEvent, GameView, Persona, Replay, SeatProfile, Potion, RuleConfig } from './types.ts';
import { StrategySelect } from './StrategySelect.tsx';
import { PlayStyleSelect } from './PlayStyleSelect.tsx';
import { useGame } from './useGame.ts';
import { RecoveryNotice } from './RecoveryNotice.tsx';
import { recoveryConfirmation } from './recovery.ts';
import type { RecoveryStrategy } from './types.ts';

function Moon() { return <svg viewBox="0 0 32 32" aria-hidden="true"><path d="M22 3A14 14 0 1 0 29 25 14 14 0 0 1 22 3Z" fill="currentColor" /></svg>; }

function Rules({ rules }: {rules?: RuleConfig}) {
  return <details className="rules"><summary>玩法说明</summary><div className="rules-content">
    <h3>{rules ? `${rules.players ?? rules.wolves + rules.seers + (rules.witches ?? 0) + rules.villagers} 人局` : '6～9 人文字局'} · 从夜晚开始</h3><p>{rules ? rosterText(rules) : '阵容可选六人、九人或自定义，以开局配置为准'}。出局不翻牌，结束才揭晓身份。</p>
    <p>狼人先各自提出目标和理由（150 字以内）；一致直接执行，不一致各回应一次（150 字以内），再秘密投目标。最终票型平票由引擎随机裁决。独狼直接选目标。</p>
    <p>预言家每夜查验一名其他存活玩家，只知道是否为狼人；当夜被刀仍能完成查验。</p>
    {(rules ? (rules.witches ?? 0) > 0 : true) && <p>加入女巫时：解药、毒药各一次，每夜最多用一种，不能自救或毒自己。解药未用时才知道当夜刀口；被刀仍可用毒。天亮统一结算，不公布死因。</p>}
    {(rules ? (rules.hunters ?? 0) > 0 : true) && <p>猎人：被刀或放逐后可开一枪，也可放弃；被毒不能开枪。开枪窗口公开身份，完成后才判胜负。</p>}
    {(rules ? (rules.guards ?? 0) > 0 : true) && <p>守卫：每夜守一人，可自守、空守，不能连续守同一人。挡刀不挡毒；救守同目标不抵消。</p>}
    {(rules ? (rules.knights ?? 0) > 0 : true) && <p>骑士：白天轮到发言前可决斗，每局一次。中狼则目标出局并立即入夜，否则骑士出局、继续白天。跳过保留技能。</p>}
    <p>白天依次发言（300 字以内），再秘密投票，可弃票，不可自投。平票者辩论，其他存活者复投一次；仍平票则无人出局。</p>
    <p>狼人全部出局，好人获胜。{wolfVictoryText(rules)}神职包括预言家、女巫、猎人、守卫和骑士。人类出局后可继续观看公开过程。</p>
    <p className="muted">离线局使用程序模拟，不代表真实模型水平；真实模型对局需合格配置及单独的费用确认。</p>
  </div></details>;
}

function Recovery({ initialId = '', onRecovered }: { initialId?: string; onRecovered: (id: string) => void }) {
  const [gameId, setGameId] = useState(initialId), [token, setToken] = useState('');
  const [error, setError] = useState(''), [busy, setBusy] = useState(false);
  return <details className="recovery" open={Boolean(initialId)}><summary>恢复已有对局</summary>
    <form onSubmit={async e => {
      e.preventDefault(); setBusy(true); setError('');
      try {
        const result = await request<GameView>(`/api/games/${encodeURIComponent(gameId.trim())}/session`, { token: token.trim() });
        setToken(''); onRecovered(result.game_id);
      } catch (err) { setError(err instanceof Error ? err.message : '恢复失败。'); }
      finally { setBusy(false); }
    }}><p className="muted">刷新通常会自动恢复。换浏览器或丢失 Cookie 后，需使用开局时保存的私密凭据；不能覆盖未结束对局。</p>
      <label>对局编号<input required value={gameId} onChange={e => setGameId(e.target.value)} autoComplete="off" /></label>
      <label>恢复凭据<input required type="password" value={token} maxLength={256} autoComplete="off" onChange={e => setToken(e.target.value)} /></label>
      {error && <p role="alert" className="error-text">{error}</p>}
      <button disabled={busy} className="secondary">{busy ? '恢复中…' : '恢复此局'}</button>
    </form>
  </details>;
}

function Lobby({ onCreated, onRecovered, account, tab, setTab, openSettings }: { onCreated: (id: string, token: string) => void; onRecovered: (id: string) => void; account: Account; tab: 'offline' | 'models'; setTab: (tab: 'offline' | 'models') => void; openSettings: () => void }) {
  const [humanSeat, setHumanSeat] = useState(1), [seed, setSeed] = useState('');
  const [roster, setRoster] = useState<Roster>(sixRoster);
  const [profiles, setProfiles] = useState<SeatProfile[]>(Array.from({length: 6}, (_, i) => ({
    seat: i + 1, nickname: `玩家 ${i + 1}`, connection_id: 'shared-mock', persona: defaultPersona(defaultPlayStyles[Math.max(0, i - 1)]),
  })));
  const [busy, setBusy] = useState(false), [error, setError] = useState('');
  const changeRoster = (r: Roster) => {
    setRoster(r); setHumanSeat(s => Math.min(s, r.players));
    setProfiles(list => seatNumbers(r.players).map(s => list.find(p => p.seat === s) ?? {
      seat:s, nickname:`玩家 ${s}`, connection_id:'shared-mock', persona:defaultPersona(defaultPlayStyles[Math.max(0, s - 2)]),
    })); setError('');
  };
  const update = (seat: number, value: Partial<SeatProfile>) => setProfiles(list => list.map(p => p.seat === seat ? {...p, ...value} : p));
  const persona = (seat: number, value: Partial<Persona>) => {
    const current = profiles.find(p => p.seat === seat)!; update(seat, {persona: {...current.persona, ...value}});
  };
  return <div className="lobby-grid">
    <section className="intro"><span className="eyebrow">ONE TABLE. MANY MINDS.</span>
      <h1>天黑以后，<br />谁的故事<br /><em>值得相信？</em></h1>
      <p className="intro-copy">你与独立的 AI 玩家同桌，人数由你选择。<br />听他们发言，找到破绽，也藏好你的秘密。</p>
      {tab === 'offline' && <p className="roster-summary">{rosterText(ruleConfig(roster))}</p>}
      <div className="offline-note"><span className="status-dot" />第一次来？先选择怎么玩<p>只体验流程，选“离线试玩”；想让你保存的 API 真正上桌，选“真实模型对局”。保存 API 不会自动开局，页面会带你完成下一步。</p><button className="secondary" onClick={() => setTab('models')}>用我的 API 配置真实玩家 →</button></div>
      <Rules />
    </section>
    <section className="setup panel"><div className="section-heading"><div><span className="eyebrow">NEW TABLE</span><h2>准备入座</h2></div><span className="badge">6～9 人 · 文字局</span></div>
      <div className="lobby-mode-tabs" role="tablist" aria-label="开局配置方式"><button role="tab" aria-label="离线试玩" aria-controls="lobby-configuration" aria-selected={tab === 'offline'} onClick={() => setTab('offline')}><b>离线试玩</b><small>程序模拟 · 不用 API · 不收费</small></button><button role="tab" aria-label="真实模型对局" aria-controls="lobby-configuration" aria-selected={tab === 'models'} onClick={() => setTab('models')}><b>真实模型对局</b><small>使用你的 API · 调用可能收费</small></button></div>
      <div id="lobby-configuration" role="tabpanel" aria-label={tab === 'models' ? '真实模型对局配置' : '离线试玩配置'}>{tab === 'models' ? <TablePlanner account={account} onCreated={onCreated} openSettings={openSettings} /> : <><p className="mode-explanation">这里的其他玩家是程序模拟，不会使用你保存的 API。想让 DeepSeek、千问、混元或 GLM-5.3 上桌，请切换上方“真实模型对局”。</p><form onSubmit={async e => {
        e.preventDefault(); setError('');
        if (rosterError(roster)) {setError(rosterError(roster)); return;}
        if (seed && (!/^-?\d+$/.test(seed) || !Number.isSafeInteger(Number(seed)))) { setError('种子需为安全范围内的整数，留空可随机。'); return; }
        setBusy(true);
        try {
          const result = await request<{game: GameView; session_token: string}>('/api/games', {
            mode: 'mock', roster, ...(seed ? {seed: Number(seed)} : {}), human_seat: humanSeat,
            ai_profiles: profiles.filter(p => p.seat !== humanSeat),
          });
          onCreated(result.game.game_id, result.session_token);
        } catch (err) { setError(err instanceof Error ? err.message : '开局失败。'); }
        finally { setBusy(false); }
      }}>
        <RosterSettings value={roster} onChange={changeRoster} />
        <div className="two-fields"><label>你的席位<select value={humanSeat} onChange={e => setHumanSeat(Number(e.target.value))}>
          {profiles.map(p => <option key={p.seat} value={p.seat}>{p.seat} 号位</option>)}
        </select></label><label>随机种子 <span className="muted">可选</span><input placeholder="留空随机" inputMode="numeric" value={seed} onChange={e => setSeed(e.target.value)} /></label></div>
        <p className="helper">身份随机分配；席位不是身份。填写种子可复现离线测试。</p>
        <div className="connection-row"><span className="connection-icon">↗</span><div><b>共享模拟连接</b><small>offline-policy · {roster.players - 1} 个独立 Player Agent</small></div><span className="badge">MOCK</span></div>
        <details className="offline-seat-settings"><summary>调整 {roster.players - 1} 个模拟玩家（可选）</summary><div className="seat-configs">{profiles.filter(p => p.seat !== humanSeat).map(p => <div className="seat-config" key={p.seat}>
          <span className="seat-number">{p.seat.toString().padStart(2, '0')}</span>
          <label className="nickname-label">{p.seat} 号昵称<input maxLength={32} value={p.nickname} onChange={e => update(p.seat, {nickname: e.target.value})} /></label>
          <label className="persona-label">{p.seat} 号个性<select aria-label={`${p.seat} 号个性`} value={p.persona.label} onChange={e => persona(p.seat, {label: e.target.value, description: presets[e.target.value] ?? p.persona.description})}>
            {[...Object.keys(presets), '自定义'].map(name => <option key={name}>{name}</option>)}
          </select></label>
          <StrategySelect label={`${p.seat} 号决策风格`} value={p.persona.strategy} onChange={strategy => persona(p.seat,{strategy})} />
          <PlayStyleSelect label={`${p.seat} 号玩法倾向`} value={p.persona.play_style} onChange={play_style => persona(p.seat,{play_style})} />
          <details className="persona-details"><summary>调整玩法倾向</summary><div className="persona-controls">
            <label>{p.seat} 号个性描述<textarea value={p.persona.description} maxLength={2000} rows={2} onChange={e => persona(p.seat, {label: '自定义', description: e.target.value})} /></label>
            <div className="two-fields"><label>风险偏好<select value={p.persona.risk} onChange={e => persona(p.seat, {risk: e.target.value as Persona['risk']})}><option value="cautious">谨慎</option><option value="balanced">均衡</option><option value="aggressive">激进</option></select></label>
              <label>诈身份倾向<select value={p.persona.bluff} onChange={e => persona(p.seat, {bluff: e.target.value as Persona['bluff']})}><option value="low">低</option><option value="medium">中</option><option value="high">高</option></select></label></div>
          </div></details>
        </div>)}</div></details>
        <p className="helper">个性配置会进入请求并保存；当前模拟策略不演绎个性。同一连接不共享身份、记忆或私密信息。</p>
        {error && <p role="alert" className="error-text">{error}</p>}
        <button className="primary start-button" disabled={busy || Boolean(rosterError(roster))}>{busy ? '正在准备牌桌…' : '开始离线对局'}<span aria-hidden="true">→</span></button>
      </form><Recovery onRecovered={onRecovered} /></>}</div>
    </section>
  </div>;
}

function EventList({ events, privateLog = false }: { events: GameEvent[]; privateLog?: boolean }) {
  if (!events.length) return <p className="empty">{privateLog ? '暂时没有私密消息。' : '静候天亮，公开记录会出现在这里。'}</p>;
  return <ol className="event-list">{events.map((event, i) => {
    const item = describeEvent(event);
    return <li key={event.id ?? i} className={event.kind === 'speak' ? 'speech-event' : 'system-event'}>
      <div className="event-meta"><b>{item.title}</b><span>第 {event.day} 天{privateLog || isPrivate(event) ? ' · 私密' : ''}</span></div>
      <p>{item.text}</p>
    </li>;
  })}</ol>;
}

function ActionPanel({ spec, busy, introPending = false, submit }: { spec: ActionSpec; busy: boolean; introPending?: boolean; submit: (action: Action) => Promise<boolean> }) {
  const [target, setTarget] = useState(''), [text, setText] = useState(''), [error, setError] = useState('');
  const [accepted, setAccepted] = useState(false);
  const [potion, setPotion] = useState<Potion | undefined>();
  const targetSpec = spec.kind === 'witch' ? {...spec, targets: potion && potion !== 'skip' ? spec.potions?.[potion] : undefined} : spec;
  const privateAction = ['kill', 'inspect', 'witch', 'protect', 'propose_kill', 'wolf_message'].includes(spec.kind);
  return <form className={`action-panel${spec.kind === 'witch' ? ' witch-action' : ''}${introPending ? ' speech-intro-pending' : ''}`} onSubmit={async e => {
    e.preventDefault(); setError('');
    if (introPending || busy || accepted) return;
    try { if (await submit(makeAction(spec, target, text, potion))) setAccepted(true); }
    catch (err) { setError(err instanceof Error ? err.message : '请检查输入。'); }
  }}><div className="section-heading"><h3>{actions[spec.kind]}</h3><span className={`badge ${privateAction ? 'violet' : ''}`}>{privateAction ? ['inspect','witch','protect'].includes(spec.kind) ? '仅你可见' : '仅狼队可见' : spec.kind === 'vote' ? '秘密提交' : '全桌可见'}</span></div>
    {spec.kind === 'protect' && <p className="helper">可自守、可空守，不能连续两夜守同一人。只挡刀，不挡毒；与解药保护同目标不抵消。</p>}
    {spec.kind === 'shoot' && <p className="helper">你已出局，但还有一次开枪机会。选择目标或放弃，完成后才判胜负；被毒不能开枪。</p>}
    {spec.kind === 'last_words' && <p className="helper">你有一次公开遗言，可公布此前查验或留下判断，最多 300 字。留空提交即跳过；遗言结束后进入开枪或胜负结算，不恢复投票权。</p>}
    {spec.kind === 'duel' && <p className="helper">每局一次：决斗到狼则狼出局并立即入夜，否则你自己出局、白天继续。跳过不消耗技能。</p>}
    {spec.kind === 'witch' && <><p className="helper">解药、毒药各一次，每夜限一种；不能自救。解药只能救本夜刀口。</p>
      <fieldset disabled={busy || accepted}><legend>本夜用药</legend><div className="target-options">{(Object.keys(spec.potions ?? {}) as Potion[]).map(choice => <label key={choice} className={`target-option ${potion === choice ? 'selected' : ''}`}><input type="radio" name="potion" value={choice} checked={potion === choice} onChange={() => {setPotion(choice); setTarget(''); setError('');}} />{potionNames[choice]}</label>)}</div></fieldset></>}
    {spec.kind === 'propose_kill' && <p className="helper">每名狼人先独立提案；全部提交后才向狼队公开。分歧时只协商一轮，再投最终目标。</p>}
    {spec.kind === 'wolf_message' && <p className="helper">你已看到各狼人的提案，请给出一次回应。所有回应提交后统一向狼队公开。</p>}
    {targetSpec.targets && <fieldset disabled={busy || accepted}><legend>选择目标</legend><div className="target-options">
      {targetSpec.targets.map(s => <label key={s} className={`target-option ${target === String(s) ? 'selected' : ''}`}><input type="radio" name="target" value={s} checked={target === String(s)} onChange={e => setTarget(e.target.value)} />{s} 号</label>)}
      {spec.allow_abstain && <label className={`target-option ${target === 'abstain' ? 'selected' : ''}`}><input type="radio" name="target" value="abstain" checked={target === 'abstain'} onChange={e => setTarget(e.target.value)} />{spec.kind === 'protect' ? '空守' : spec.kind === 'shoot' ? '放弃开枪' : spec.kind === 'duel' ? '跳过 · 保留技能' : '弃票'}</label>}
    </div></fieldset>}
    {spec.max_length != null && <label className="speech-label">{spec.kind === 'propose_kill' ? '刀人理由' : '发言内容'}
      <textarea rows={3} value={text} disabled={busy || accepted} placeholder={privateAction ? '写给狼队的判断…' : '告诉大家你的判断；身份声明只是发言，不是事实。'} onChange={e => { setText(e.target.value); setError(''); }} />
      <span className={`char-count ${countChars(text) > spec.max_length ? 'error-text' : ''}`}>{countChars(text)} / {spec.max_length} 字</span></label>}
    {error && <p className="error-text" role="alert">{error}</p>}
    {introPending && <p className="helper" role="status">白天发言提示播放中，可先写内容；播完或跳过动画后提交。</p>}
    <button className="primary" disabled={busy || accepted || introPending}>{accepted ? '已提交，等待局面更新…' : introPending ? '等待发言提示…' : busy ? '正在提交…' : spec.kind === 'last_words' && !text.trim() ? '跳过遗言' : actions[spec.kind]}</button>
  </form>;
}

function PrivatePanel({ game }: { game: GameView }) {
  const [revealed, setRevealed] = useState(false);
  const view = game.view;
  if (!view.alive && view.phase !== 'last_words' && game.status !== 'finished') return <section className="panel private-panel"><span className="eyebrow">SPECTATOR</span><h3>你已出局</h3><p>接下来只观看公开记录，不能再行动。其他身份与秘密将在结束后揭晓。</p></section>;
  return <section className="panel private-panel"><div className="section-heading"><div><span className="eyebrow">FOR YOUR EYES ONLY</span><h3>你的秘密</h3></div><button className="text-button" onClick={() => setRevealed(!revealed)}>{revealed ? '隐藏' : '查看身份'}</button></div>
    {!revealed ? <button className="identity-cover" onClick={() => setRevealed(true)}><Moon /><span>点击翻开身份牌</span><small>请避免旁人看到屏幕</small></button> : <div className="identity-card">
      <span className="role-icon">{view.role === 'wolf' ? '☾' : view.role === 'seer' ? '✧' : view.role === 'witch' ? '⚗' : '◇'}</span><h2>{roles[view.role]}</h2>
      <p>{view.role === 'wolf' ? wolfObjective(view.rules) : view.role === 'seer' ? '每夜查验，结合公开发言找到所有狼人。' : view.role === 'witch' ? '解药救人，毒药反击。每夜最多用一种，不能自救。' : view.role === 'hunter' ? '被刀或放逐可开一枪，被毒不能开枪。' : view.role === 'guard' ? '每夜守一人，可自守，不能连续守同一人；挡刀不挡毒。' : view.role === 'knight' ? '白天发言前可决斗一次：中狼则狼死入夜，否则自己死。' : '没有夜间技能，用发言与票型找到所有狼人。'}</p>
      {view.role === 'witch' && view.witch && <div className="check-list"><p>解药：<b>{view.witch.potions.save ? '可用 · 一次' : '已用完'}</b> · 毒药：<b>{view.witch.potions.poison ? '可用 · 一次' : '已用完'}</b></p>{view.witch.night_victim != null && <p className="private-fact">本夜被刀：{view.witch.night_victim} 号{view.witch.night_victim === view.seat ? '（你，不能自救）' : ''}</p>}</div>}
      {view.role === 'guard' && view.guard && <p className="private-fact">最近守护：{view.guard.last_target == null ? '未守护 / 空守' : `${view.guard.last_target} 号`}；合法目标列表已排除连续守护。</p>}
      {view.role === 'knight' && view.knight && <p className="private-fact">决斗技能：{view.knight.used ? '已用完' : '可用 · 每局一次'}</p>}
      {view.role === 'wolf' && <p className="private-fact">狼队友：{view.wolf_teammates.map(s => `${s} 号`).join('、')}</p>}
      {view.role === 'seer' && <div className="check-list">{view.checks.length ? view.checks.map((check, i) => <p key={i}>{check.target} 号 · <b>{check.is_wolf ? '狼人' : '好人'}</b></p>) : <p className="muted">尚无查验结果</p>}</div>}
      {view.role === 'wolf' && <p className="helper">狼队交流在牌桌的“对话”中，以“仅狼队可见”标注。</p>}
    </div>}
  </section>;
}

function SecretsDialog({ game, close }: { game: GameView; close: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const dismiss = () => { dialog.current?.close(); close(); };
  useEffect(() => {
    const element = dialog.current;
    element?.showModal();
    return () => { element?.close(); };
  }, []);
  return <dialog className="secrets-dialog" ref={dialog} aria-labelledby="secrets-title" onCancel={event => { event.preventDefault(); dismiss(); }}
    onClick={event => { if (event.target === dialog.current) dismiss(); }}>
    <div className="dialog-heading"><h2 id="secrets-title">你的秘密</h2><button className="secondary" autoFocus onClick={dismiss}>关闭</button></div>
    <PrivatePanel game={game} />
  </dialog>;
}

function ReplayPanel({ game }: { game: GameView }) {
  const [replay, setReplay] = useState<Replay | null>(null), [error, setError] = useState('');
  const [step, setStep] = useState(0), [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let cancelled = false;
    request<Replay>(`/api/games/${game.game_id}/replay`).then(result => {
      if (!cancelled) { setReplay(result); setStep(result.events.length); setError(''); }
    }).catch(err => { if (!cancelled) setError(err instanceof Error ? err.message : '复盘读取失败。'); });
    return () => { cancelled = true; };
  }, [game.game_id, attempt]);
  if (error) return <section className="panel"><p className="error-text">{error}</p><button onClick={() => setAttempt(attempt + 1)}>重新读取复盘</button></section>;
  if (!replay) return <section className="panel"><p>正在读取复盘…</p></section>;
  const download = () => {
    const blob = new Blob([JSON.stringify(replay, null, 2)], {type: 'application/json'});
    const url = URL.createObjectURL(blob), link = document.createElement('a');
    link.href = url; link.download = `werewolf-${game.game_id}.json`; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  return <section className="panel replay-panel"><div className="section-heading"><div><span className="eyebrow">THE WHOLE STORY</span><h2>赛后复盘</h2></div><button className="secondary" onClick={download}>导出 JSON</button></div>
    <p className="helper">现在可以查看完整身份和秘密事件。回放只读取存档，不产生模型调用。导出文件包含秘密，请谨慎分享。</p>
    <div className="replay-roles">{replay.players.map(p => <div key={p.seat}><span>{p.seat} 号</span><b>{roles[p.role]}</b><small>{p.alive ? '最终存活' : '最终出局'}</small></div>)}</div>
    <div className="replay-controls"><button className="secondary" disabled={step === 0} onClick={() => setStep(step - 1)}>上一条</button>
      <label>回放进度<input type="range" min={0} max={replay.events.length} value={step} onChange={e => setStep(Number(e.target.value))} /></label>
      <span>{step} / {replay.events.length}</span><button className="secondary" disabled={step === replay.events.length} onClick={() => setStep(step + 1)}>下一条</button></div>
    <div className="replay-events"><EventList events={replay.events.slice(0, step)} /></div>
    <details className="usage-details"><summary>{game.mode === 'mock' || game.simulated_transport ? '模拟' : '供应商'}调用记录 · {replay.usage.length} 次</summary>
      <p className="helper">未知用量不按零计费，缓存/推理是子项，不重复加总。费用最终以供应商账单为准；模拟记录没有实际账单。</p>
      <div className="usage-table"><table><thead><tr><th>席位</th><th>模型</th><th>结果</th><th>输入 / 输出 token</th></tr></thead><tbody>
        {replay.usage.map((record, i) => <tr key={i}><td>{record.seat} 号</td><td>{record.model ?? 'offline-policy'}</td><td>{record.status}</td><td>{record.usage?.input_tokens ?? '未知'} / {record.usage?.output_tokens ?? '未知'}</td></tr>)}
      </tbody></table></div></details>
  </section>;
}

export function App() {
  const session = useGame();
  const [account, setAccount] = useState<Account>({user:null, csrf_token:null});
  const [lobbyTab, setLobbyTab] = useState<'offline' | 'models'>('offline');
  const [settingsRequest, setSettingsRequest] = useState(0);
  const [token, setToken] = useState('');
  const [showToken, setShowToken] = useState(false);
  const [secretsOpen, setSecretsOpen] = useState(false);
  const [hideSecrets, setHideSecrets] = useState(false);
  const [visibleHeight, setVisibleHeight] = useState<number | undefined>();
  useEffect(() => {
    const viewport = window.visualViewport;
    const resize = () => setVisibleHeight(viewport?.height);
    resize(); viewport?.addEventListener('resize', resize);
    return () => viewport?.removeEventListener('resize', resize);
  }, []);
  const game = session.game, view = game?.view;
  const terminal = game?.status === 'finished' || game?.status === 'abandoned';
  const [typingBusy, setTypingBusy] = useState(false);
  const [speechIntroBusy, setSpeechIntroBusy] = useState(false);
  useEffect(() => { setTypingBusy(false); }, [game?.game_id]);
  useEffect(() => { setSecretsOpen(false); }, [game?.game_id, view?.alive]);
  useEffect(() => { setHideSecrets(false); if (game) window.scrollTo(0, 0); }, [game?.game_id]);
  const control = async (kind: 'pause' | 'resume' | 'abandon', recovery?: RecoveryStrategy) => {
    if (!game || session.pending) return;
    if (kind === 'resume' && game.mode === 'real' && !window.confirm(recovery && game.decision_recovery
      ? recoveryConfirmation(game.decision_recovery, recovery) : '恢复真实对局？将继续调用供应商（可能包括下一步预取），可能产生费用。')) return;
    if (kind === 'abandon' && !window.confirm('结束本局且不判胜负？存档和调用记录保留，停止后续调用；已发出的请求仍可能计费。')) return;
    await session.mutate({path: `/api/games/${game.game_id}/${kind}`, body: {operation_id: crypto.randomUUID(), ...(recovery ? {recovery} : {})}});
  };
  return <div className={`app-shell ${game ? 'in-game' : ''} ${game?.status === 'finished' ? 'finished-table' : ''}`} style={visibleHeight ? { '--visible-height': `${visibleHeight}px` } as CSSProperties : undefined}><header className="app-header"><a href="/" className="brand" onClick={e => e.preventDefault()}><Moon /><span>夜谈<small>LLM WEREWOLF</small></span></a>
    <div className="header-right"><span className="badge"><span className="status-dot" />{!game ? '准备入座' : game.mode === 'real' ? game.simulated_transport ? '隔离接口模拟' : '真实模型对局' : '离线模拟'}</span><AccountSettings onAccountChange={setAccount} openRequest={settingsRequest} onConfigurePlayers={!session.id ? () => setLobbyTab('models') : undefined} /><a className="docs-link" href="/docs" target="_blank" rel="noreferrer">开发文档 ↗</a></div></header>
    <main>
      {!session.id ? <Lobby account={account} tab={lobbyTab} setTab={setLobbyTab} openSettings={() => setSettingsRequest(n => n + 1)} onCreated={(id, credential) => { setToken(credential); setShowToken(false); session.setActive(id); }} onRecovered={id => session.setActive(id)} /> : !game ?
        <section className="panel loading-panel"><h2>{session.readError ? '暂时无法进入牌桌' : '正在恢复牌桌…'}</h2>
          {session.readError && <p role="alert" className="error-text">{session.readError}</p>}
          <p className="muted">已有对局会自动恢复，不会因刷新创建新局或重复行动。</p>
          <button className="secondary" onClick={() => { void session.refresh(); }}>重新读取</button>
          {session.unauthorized && <Recovery initialId={session.id} onRecovered={() => { void session.refresh(); }} />}
          <button className="text-button" onClick={() => session.setActive('')}>返回开局页（不删除存档）</button>
        </section> : <>
          <div className="table-workspace"><div className="conversation-workspace"><section className="table-heading"><div><span className="eyebrow">TABLE {game.game_id.slice(0, 6).toUpperCase()}</span>
            <h1>{game.status === 'abandoned' ? '本局已结束 · 未判胜负' : game.status === 'finished' ? view!.winner === 'good' ? '好人阵营获胜' : '狼人阵营获胜' : game.preparation?.active ? '拿牌准备中' : `第 ${view!.day} 天 · ${phases[presentationPhase(game, typingBusy)] ?? view!.phase}`}</h1>
            <p className={`muted ${game.turn_version && !typingBusy ? 'your-turn' : ''}`}>{waitingText(game, typingBusy)}</p></div>
            <div className="table-controls"><span className={`connection-status ${session.connected ? '' : 'disconnected'}`}><span className="status-dot" />{session.connected ? '已连接' : '正在重连'}</span>
              {(view!.alive || (view!.phase === 'last_words' && view!.checks.length > 0) || game.status === 'finished') && <button className="secondary secrets-entry" onClick={() => setSecretsOpen(true)}>你的秘密{view!.checks.length > 0 ? ` · ${view!.checks.length} 次查验` : ''}</button>}
              {view!.alive && view!.role === 'wolf' && <button className="text-button" onClick={() => setHideSecrets(!hideSecrets)}>{hideSecrets ? '显示狼队消息' : '隐藏狼队消息'}</button>}
              {!terminal && <label className="playback-select">推进模式<select aria-label="推进模式" value={game.playback_mode ?? 'auto'} disabled={session.busy || Boolean(session.pending)} onChange={e => { void session.mutate({path:`/api/games/${game.game_id}/playback`, body:{operation_id:crypto.randomUUID(), mode:e.target.value}}); }}><option value="manual">单步 · 默认</option><option value="auto">自动推进</option></select></label>}
              {!terminal ? !(game.status === 'paused' && game.decision_recovery) && <button className="secondary" disabled={session.busy || Boolean(session.pending)} onClick={() => { void control(game.status === 'paused' ? 'resume' : 'pause'); }}>{game.status === 'paused' ? '恢复运行' : '完全暂停'}</button> : <button className="primary" onClick={() => { setToken(''); session.setActive(''); }}>开新局</button>}
              {game.status === 'paused' && <button className="text-button" disabled={session.busy || Boolean(session.pending)} onClick={() => {void control('abandon');}}>结束本局</button>}
            </div></section>
          {session.readError && <div className="notice error-notice" role="alert">{session.readError}</div>}
          {session.notice && <div className="notice error-notice" role="alert">{session.notice}</div>}
          {session.pending && <div className="notice"><p>上次操作的结果尚未确认。重试会使用同一个操作编号，不会重复结算。</p><button className="secondary" disabled={session.busy} onClick={() => { void session.mutate(session.pending!); }}>重试上次操作</button></div>}
          {game.status === 'paused' && <div className="notice pause-notice"><b>对局已暂停</b><span>{pauseReason(game.pause_reason)}</span></div>}
          {game.status === 'paused' && game.decision_recovery && <RecoveryNotice recovery={game.decision_recovery}
            busy={session.busy || Boolean(session.pending) || !session.connected} resume={strategy=>{void control('resume', strategy);}} />}
          {game.mode === 'real' && game.status === 'paused' && game.pause_reason === 'account_session_expired' && account.user && <button className="secondary" onClick={async () => {
            if (!window.confirm('为本局恢复当前账号授权？不会立即调用模型；随后手动恢复运行可能产生费用。')) return;
            try {await request(`/api/account/games/${game.game_id}/authorize`, {confirm_cost:true}, {'X-CSRF-Token':account.csrf_token ?? ''}); await session.refresh(); session.setNotice('账号授权已恢复；点击“恢复运行”才继续。');}
            catch (err) {session.setNotice(err instanceof Error ? err.message : '恢复授权失败');}
          }}>恢复账号授权（不立即调用）</button>}
          </div><aside className={`panel seats-panel${view!.players.length > 6 ? ' many-seats' : ''}`} aria-label={`${view!.players.length} 人牌桌`}><div className="section-heading"><h2>{view!.players.length} 人牌桌</h2><span className="muted">{view!.players.filter(p => p.alive).length} / {view!.players.length} 存活</span></div>
            <div className="player-grid">{view!.players.map(p => {
              const profile = game.seat_profiles.find(s => s.seat === p.seat), mine = p.seat === game.human_seat;
              const revealedRole = view!.revealed_roles?.[String(p.seat)];
              return <article className={`player-card ${mine ? 'mine' : ''} ${!p.alive ? 'dead' : ''} ${view!.current_actor === p.seat ? 'acting' : ''}`} key={p.seat}>
                <Avatar seat={p.seat} /><div className="seat-info"><h3 title={mine ? '你' : profile?.nickname}>{p.seat} 号 · {mine ? '你' : profile?.nickname || `玩家 ${p.seat}`}</h3>
                <span className="player-state">{!p.alive ? '已出局' : view!.current_actor === p.seat ? '行动中' : '存活'}</span>
                {revealedRole && <p>{roles[revealedRole]}</p>}</div>
              </article>;
            })}</div></aside><div className="conversation-body">
            <Chat key={game.game_id} game={game} hideSecrets={hideSecrets} onTypingChange={setTypingBusy} onSpeechIntroChange={setSpeechIntroBusy} />
<section className="panel action-container">{game.status === 'abandoned' ? <div className="waiting"><h3>本局已结束</h3><p>未判胜负。存档和用量保留，不再调用模型，不揭晓完整秘密。</p></div> : game.status === 'finished' ? <div className="waiting"><span>✧</span><h3>身份已揭晓</h3><p>下方可逐条查看完整复盘。</p></div> : game.status === 'paused' ? <div className="waiting"><span>Ⅱ</span><h3>已完全暂停</h3><p>{game.decision_recovery ? '请在上方选择本次增加额度或关闭思考继续。局面已保留。' : '不启动新调用；已有调用可能已产生用量。点击“恢复运行”回到原模式。'}</p></div> : game.preparation?.active ? <div className="waiting" role="status"><h3>玩家正在拿牌准备 · {game.preparation.completed}/{game.preparation.total}</h3><p>你可以先查看自己的身份。准备结束后开始本局。</p></div> : game.step_waiting ? <div className="step-wait"><div><h3>{typingBusy ? '这一位还在说话' : '本步已展示，等你继续'}</h3><p>后台最多准备下一步，结果暂不显示、不结算。完全暂停可停止后续调用。</p></div><button className="primary" disabled={typingBusy || session.busy || Boolean(session.pending) || !session.connected} onClick={() => { void session.mutate({path:`/api/games/${game.game_id}/advance`, body:{operation_id:crypto.randomUUID(), expected_step:game.step_id}}); }}>继续下一步 →</button></div> : !view!.alive && !['shoot','last_words'].includes(view!.legal_actions[0]?.kind ?? '') ? <div className="waiting"><span>◇</span><h3>公开观战中</h3><p>你不能再发言、投票或执行夜间行动。</p></div> : game.turn_version && view!.legal_actions[0] ?
              view!.legal_actions[0].kind === 'vote' && typingBusy ? <div className="waiting"><h3>请等所有玩家说完</h3><p>最后的发言展示完后再开放投票。</p></div> : <ActionPanel key={game.turn_version} spec={view!.legal_actions[0]} introPending={view!.phase === 'day_speech' && speechIntroBusy} busy={session.busy || Boolean(session.pending) || !session.connected} submit={action => session.mutate({path: `/api/games/${game.game_id}/actions`, body: {operation_id: crypto.randomUUID(), expected_version: game.turn_version, action}})} /> :
              <div className="waiting"><span className="waiting-moon"><Moon /></span><h3>{game.reading ? '等上一位说完' : '等待其他玩家行动'}</h3><p>{game.reading ? '留出吐字和阅读时间后，再开始下一位的决策。' : '局面会自动更新，无需刷新。夜间秘密与未公布选票不会显示。'}</p></div>}</section>
          </div></div>
          {secretsOpen && (view!.alive || view!.phase === 'last_words' || game.status === 'finished') && <SecretsDialog key={game.game_id} game={game} close={() => setSecretsOpen(false)} />}
          <details className="panel table-tools"><summary>牌桌工具 · 玩法说明与恢复信息</summary><Rules rules={view?.rules} />
            {game.status === 'paused' && <button className="secondary" onClick={() => session.setActive('')}>返回开局页（保留暂停存档）</button>}
            <p className="helper">你的行动只交给引擎校验。AI 无权修改规则，也看不到你的私密视图。</p>
            <details className="seat-profile-details"><summary>席位模型与个性</summary>{game.seat_profiles.map(profile => <p className="helper" key={profile.seat}>{profile.seat} 号 · {profile.nickname} · {profile.model ?? 'offline-policy'} · {profile.persona.label} · {playStyleNames[profile.persona.play_style ?? 'serious']}</p>)}</details>
            <details className="session-details"><summary>保存恢复信息（私密）</summary><p className="helper">当前浏览器通过 Cookie 自动恢复。若需换浏览器，请保存对局编号及开局时的凭据。不要公开分享。</p><label>对局编号<input readOnly value={game.game_id} /></label>
              {token ? <div><label>恢复凭据<input readOnly type={showToken ? 'text' : 'password'} value={token} onFocus={e => e.target.select()} /><small>凭据仅在本次开局页面中保留，刷新后不再显示。</small></label><button className="text-button" onClick={() => setShowToken(!showToken)}>{showToken ? '隐藏恢复凭据' : '显示恢复凭据以便复制'}</button></div> : <p className="helper">凭据不从后端重新返回；本浏览器可继续使用原 Cookie。</p>}
            </details></details>
          {game.status === 'finished' && <ReplayPanel game={game} />}
          {game.mode === 'real' && <GameUsage key={game.game_id} game={game} />}
        </>}
    </main><footer><span>夜谈 · 不同的模型，同一张牌桌。</span><span>阶段 5 · 模型接入</span></footer>
  </div>;
}
