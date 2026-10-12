import { useEffect, useRef, useState } from 'react';
import type { Account, SavedConnection } from './account.ts';
import { playableConnection, providers } from './account.ts';
import { ApiError, request } from './api.ts';
import { Avatar } from './Avatar.tsx';
import { presets } from './game.ts';
import { qualificationError } from './qualification.ts';
import { admissionReady, seatReady, admissionStatus, applyConnection, changeHuman, changeRoster, diversifyStrategies, diversifyPlayStyles, emptyPlan, planBody, realStartBody, thinkingLimitsText } from './table-plan.ts';
import { StrategySelect } from './StrategySelect.tsx';
import { PlayStyleSelect } from './PlayStyleSelect.tsx';
import { RosterSettings } from './RosterSettings.tsx';
import { seatNumbers, sixRoster } from './roster.ts';
import type { Admission, PlanSeat, SavedPlan, TablePlan } from './table-plan.ts';
import type { GameView, Persona } from './types.ts';
import { qualificationStartUncertain } from './qualification.ts';
import { plannerGuide } from './onboarding.ts';

type Created = {game: GameView; session_token: string};
type CreateHandler = (id: string, token: string) => void;

function SetupSteps({step}: {step: number}) {
  return <ol className="setup-steps" aria-label="真实对局准备步骤">{['保存 API', '配置席位', '检查配置', '确认开局'].map((title, i) => <li key={title} className={i + 1 < step ? 'done' : ''} aria-current={i + 1 === step ? 'step' : undefined}><span aria-hidden="true">{i + 1 < step ? '✓' : i + 1}</span><b>{title}</b>{i + 1 === step && <small>当前步骤</small>}</li>)}</ol>;
}

export function TablePlanner({account, onCreated, openSettings}: {account: Account; onCreated: CreateHandler; openSettings: () => void}) {
  if (!account.user) return <div className="planner-empty"><SetupSteps step={1} /><section className="setup-next"><span className="eyebrow">下一步 · 01</span><h3>先登录并保存你的 API</h3><p>设置只保存连接，不会开局，也不会调用模型。已有账号直接登录；已有 API 不用重新填写。</p><button className="primary" onClick={openSettings}>登录 / 保存 API →</button></section><p className="helper">回来把连接分配给 AI，检查配置后明确确认费用才开局。不需要资格考试。</p></div>;
  return <SignedInPlanner key={account.user.id} account={account} onCreated={onCreated} openSettings={openSettings} />;
}

function SignedInPlanner({account, onCreated, openSettings}: {account: Account; onCreated: CreateHandler; openSettings: () => void}) {
  const [plan, setPlan] = useState<TablePlan>(emptyPlan), [seed, setSeed] = useState('');
  const roster = plan.roster ?? sixRoster(), aiCount = roster.players - 1;
  const [version, setVersion] = useState<string | null>(null), [connections, setConnections] = useState<SavedConnection[]>([]);
  const [bulk, setBulk] = useState(''), [admission, setAdmission] = useState<Admission | null>(null);
  const [ready, setReady] = useState(false), [busy, setBusy] = useState(false), [invalidSession, setInvalidSession] = useState(false);
  const [error, setError] = useState(''), [notice, setNotice] = useState(''), [reload, setReload] = useState(0);
  const [confirmCost, setConfirmCost] = useState(false), [uncertain, setUncertain] = useState(false);
  const [existing, setExisting] = useState<Created | null>(null);
  const [recent, setRecent] = useState<Created | null>(null);
  const [showSeats, setShowSeats] = useState(false);
  const connectionsRef = useRef<HTMLDivElement>(null), seatsRef = useRef<HTMLDetailsElement>(null), consentRef = useRef<HTMLDivElement>(null);
  const guideRef = useRef<HTMLElement>(null);
  const pendingStart = useRef<Record<string, unknown> | null>(null);
  const loaded = useRef(false), epoch = useRef(0), restore = useRef(false);
  useEffect(() => {
    const current = ++epoch.current;
    setBusy(false); setAdmission(null); setConfirmCost(false); setError(''); setReady(false);
    const load = async () => {
      try {
        const list = await request<{connections: SavedConnection[]}>('/api/account/connections');
        const draft = !loaded.current || restore.current ? await request<SavedPlan>('/api/account/table-plan') : null;
        const active = await request<{game: GameView | null; session_token: string | null; last_game: Created | null}>('/api/account/games/current');
        if (epoch.current !== current) return;
        setConnections(list.connections); setInvalidSession(false);
        setExisting(active.game && active.session_token ? {game:active.game, session_token:active.session_token} : null);
        setRecent(active.last_game);
        if (draft) {
          setPlan(draft.plan ?? emptyPlan()); setSeed(draft.plan?.seed?.toString() ?? ''); setVersion(draft.version);
          loaded.current = true; restore.current = false;
        }
        setReady(true);
      } catch (err) {
        if (epoch.current !== current) return;
        setError(err instanceof Error ? err.message : '读取配置失败');
        if (err instanceof ApiError && err.status === 401) {setInvalidSession(true); setConnections([]); setPlan(emptyPlan()); setSeed('');}
      }
    };
    void load();
    return () => {++epoch.current;};
  }, [account, reload]);
  const post = <T,>(path: string, body: unknown) => request<T>(path, body, {'X-CSRF-Token':account.csrf_token ?? ''});
  const edit = (next: TablePlan) => {setPlan(next); setAdmission(null); setConfirmCost(false); setNotice('当前配置已修改，请重新检查开局配置，不调用模型。'); setError('');};
  const update = (seat: number, value: Partial<PlanSeat>) => edit({...plan, ai_profiles: plan.ai_profiles.map(p => p.seat === seat ? {...p, ...value} : p)});
  const persona = (p: PlanSeat, value: Partial<Persona>) => update(p.seat, {persona:{...p.persona,...value}});
  const operate = async (save: boolean) => {
    const current = epoch.current;
    setError(''); setNotice(''); setBusy(true); setAdmission(null); setConfirmCost(false);
    try {
      const body = planBody(plan, seed);
      if (save) {
        const result = await post<SavedPlan>('/api/account/table-plan', {plan:body, expected_version:version});
        if (epoch.current !== current) return;
        setVersion(result.version); setNotice('配置已保存到本机账号。未开局，也未调用模型。');
      } else {
        const result = await post<Admission>('/api/account/table-plan/admission', body);
        if (epoch.current !== current) return;
        setAdmission(result); setNotice('已检查当前表单；不保存修改、不调用模型，也不验证模型实际效果。');
      }
    } catch (err) {
      if (epoch.current !== current) return;
      setError(err instanceof Error ? err.message : '操作失败');
      if (err instanceof ApiError && err.status === 401) {setInvalidSession(true); setConnections([]); setPlan(emptyPlan()); setSeed('');}
    } finally {if (epoch.current === current) setBusy(false);}
  };
  const startGame = async () => {
    if (busy || (!pendingStart.current && (!admissionReady(admission) || !confirmCost))) return;
    const current = epoch.current;
    try {
      const body = pendingStart.current ?? realStartBody(plan, seed, admission!, crypto.randomUUID(), confirmCost);
      pendingStart.current = body; setBusy(true); setError('');
      const result = await post<Created>('/api/account/games', body);
      if (epoch.current !== current) return;
      pendingStart.current = null; setUncertain(false); onCreated(result.game.game_id,result.session_token);
    } catch (err) {
      if (epoch.current !== current) return;
      if (err instanceof ApiError && !qualificationStartUncertain(err.status)) {
        pendingStart.current = null; setUncertain(false); setAdmission(null); setConfirmCost(false); setError(err.message);
      } else {setUncertain(true); setError('开局结果尚未确认。重试使用原开局编号，不重复创建；刷新后也可恢复已有真实对局。');}
    } finally {if (epoch.current === current) setBusy(false);}
  };
  const guide = plannerGuide(plan, connections, admission, ready, Boolean(existing), uncertain);
  const thinkingSeats = admission?.seats.filter(s => s.thinking_mode === 'low') ?? [];
  const guideAction = () => {
    if (guide.action === 'settings') openSettings();
    else if (guide.action === 'connections') connectionsRef.current?.scrollIntoView({block:'center'});
    else if (guide.action === 'preflight') void operate(false);
    else if (guide.action === 'consent') consentRef.current?.scrollIntoView({block:'center'});
    else if (guide.action === 'existing' && existing) onCreated(existing.game.game_id, existing.session_token);
    else if (guide.action === 'retry') void startGame();
  };
  return <div className="table-planner">
    <SetupSteps step={guide.step} />
    {!invalidSession && <section ref={guideRef} className="setup-next" aria-label="当前准备提示" aria-live="polite"><span className="eyebrow">下一步 · 0{guide.step}</span><h3>{guide.title}</h3><p>{guide.detail}</p>{guide.action && <button className="primary" disabled={busy || (uncertain && guide.action !== 'retry')} onClick={guideAction}>{guide.label}</button>}</section>}
    <p className="helper planner-free-note">保存 API → 配置席位 → 检查配置：均不调用模型。资格测试已取消；确认费用后才正式开局。</p>
    {error && <p className="error-text" role="alert">{error}</p>}{notice && <p className="account-notice" role="status">{notice}</p>}
    {invalidSession ? <p>登录已失效，请重新打开右上角设置登录。</p> : <>
      {existing && <div className="admission-summary"><h3>你有一局尚未结束的真实对局</h3><p className="helper">进入只读取已有牌桌，不创建新局；暂停后继续运行可能产生费用。</p><button className="secondary" disabled={busy} onClick={() => onCreated(existing.game.game_id, existing.session_token)}>进入已有真实对局</button></div>}
      {!existing && recent && <div className="admission-summary"><h3>最近的一局已结束</h3><p className="helper">读取存档不创建新局；若此前开局响应丢失，可在这里确认结果。</p><button className="secondary" disabled={busy} onClick={() => onCreated(recent.game.game_id,recent.session_token)}>查看最近已结束对局</button></div>}
      {!ready && <p>{error ? '请点击下方“刷新连接与配置”重试。' : '正在读取配置…'}</p>}
      {admission && <section className={`admission-summary ${admissionReady(admission) ? '' : 'needs-attention'}`} aria-label="开局配置检查结果"><h3>{admissionReady(admission) ? '所有席位配置可用' : '还有席位配置需要修改'}</h3><p>{admission.unique_connections} 个连接 · 本次 {admission.model_calls} 次模型调用</p>{thinkingSeats.length > 0 && <p>{thinkingSeats.length} 个 AI 开启思考，单次额度含思考与回答。</p>}<p className="helper">仅检查连接归属、已接入模型和请求参数，不验证密钥有效性、余额或模型效果。后端开局时会再次校验，并固定本局模型、参数、个性和规则上下文。</p></section>}
      {admissionReady(admission) && !existing && <div ref={consentRef} className="qualification-consent planner-start"><h3>第 4 步 · 确认费用，正式开局</h3><p>真实对局会调用你的 API。每个 AI 额外进行一次拿牌准备，本局共 {aiCount} 次（九人局为 8 次）；准备失败直接跳过，不自动重试。单步等待时最多提前准备一个正式行动。正式行动临时故障/无效输出最多重试两次，重试单独记账；鉴权失败直接暂停。未知用量不按免费处理。</p>{thinkingSeats.length > 0 && <p>本局启用了限额思考：神职额度 ×2，第二晚起再 ×2，合计最高 16384 tokens，可能增加等待和费用。超过 8192 的请求超时延长到 120 秒。正式行动超时或额度耗尽时暂停；可选择本次增加额度或关闭思考继续。拿牌准备失败仍跳过。</p>}<label className="check-label"><input type="checkbox" checked={confirmCost} disabled={busy || uncertain} onChange={e => setConfirmCost(e.target.checked)} />我确认开始真实模型对局，知道拿牌准备、行动及预取可能产生费用</label><button className="primary" disabled={!confirmCost || busy || uncertain} onClick={() => {void startGame();}}>开始真实模型对局</button><p className="helper">未勾选时不能开局；页面不会替你自动确认。</p></div>}
      <fieldset disabled={!ready || busy || uncertain} className="planner-fields"><legend className="sr-only">模型席位配置</legend>
        <RosterSettings value={roster} onChange={r => edit(changeRoster(plan, r, connections.some(c => c.id === bulk) ? bulk : ''))} />
        <div className="two-fields"><label>人类席位<select value={plan.human_seat} onChange={e => edit(changeHuman(plan, Number(e.target.value), connections.some(c => c.id === bulk) ? bulk : ''))}>{seatNumbers(roster.players).map(s => <option key={s} value={s}>{s} 号 · 你</option>)}</select></label><label>随机种子（可选）<input value={seed} inputMode="numeric" placeholder="留空随机" onChange={e => {setSeed(e.target.value); setAdmission(null); setNotice('有未保存的修改。');}} /></label></div>
        <p className="helper">席位不等于身份，开局时才随机发牌。</p>
        <button type="button" className="secondary" onClick={() => {edit(diversifyPlayStyles(plan));setShowSeats(true);}}>一键混搭玩法倾向</button>
        <p className="helper">认真、直觉、观望与整活混搭，影响发言和行动。每个玩家仍想让自己阵营赢，可以冒险或改主意。</p>
        <button type="button" className="secondary" onClick={() => {edit(diversifyStrategies(plan));setShowSeats(true);}}>一键分配不同决策风格</button>
        <p className="helper">只改判断侧重点，保留模型、昵称和说话个性。不需要考试，不会自动调用模型；修改后重新检查配置即可。</p>
        <div ref={connectionsRef} className="planner-connections"><h3>第 2 步 · 为 {aiCount} 个 AI 选择 API</h3><p className="helper">已经保存过？这里直接选，不用再填密钥。多个玩家使用同一 API，仍是不同角色、不同私密视图。</p><div className="bulk-connection"><label>快速使用同一 API<select value={bulk} onChange={e => setBulk(e.target.value)}><option value="">选择已保存连接</option>{connections.map(c => <option key={c.id} value={c.id}>{c.label} · {c.model}{playableConnection(c) ? '' : '（暂不可参赛）'}</option>)}</select></label><button type="button" className="secondary" disabled={!connections.some(c => c.id === bulk && playableConnection(c))} onClick={() => edit(applyConnection(plan, bulk))}>应用到 {aiCount} 个 AI</button></div></div>
        {!connections.length && <p className="helper">还没有连接。先在右上角设置添加，再回来配置。</p>}
        <details ref={seatsRef} className="planner-seat-settings" open={showSeats} onToggle={e => setShowSeats(e.currentTarget.open)}><summary>查看 {aiCount} 个 AI / 分别改个性 <small>{plan.ai_profiles.filter(p => connections.some(c => c.id === p.connection_id && playableConnection(c))).length} / {aiCount} 已选连接{admission ? ` · ${admission.seats.filter(seatReady).length} / ${aiCount} 配置可用` : ''}</small></summary><p className="helper">可自由调整个性与决策风格，不需要重测；本局开始后配置固定，不会自动切换模型。</p><div className="planned-seats">{plan.ai_profiles.map(p => {
          const c = connections.find(c => c.id === p.connection_id), result = admission?.seats.find(s => s.seat === p.seat);
          return <article key={p.seat} className="planned-seat"><div className="planned-seat-heading"><Avatar seat={p.seat} /><div><b>{p.seat} 号 · AI 玩家</b><span className={`seat-admission ${seatReady(result) ? 'valid' : ''}`}>{admissionStatus(result)}</span></div></div>
            <div className="two-fields"><label>{p.seat} 号昵称<input maxLength={32} value={p.nickname} onChange={e => update(p.seat, {nickname:e.target.value})} /></label><label>{p.seat} 号个性<select value={p.persona.label} onChange={e => persona(p, {label:e.target.value, description:presets[e.target.value] ?? p.persona.description})}>{[...new Set([...Object.keys(presets), '自定义', p.persona.label])].map(n => <option key={n}>{n}</option>)}</select></label></div>
            <StrategySelect label={`${p.seat} 号决策风格`} value={p.persona.strategy} onChange={strategy => persona(p,{strategy})} />
            <PlayStyleSelect label={`${p.seat} 号玩法倾向`} value={p.persona.play_style} onChange={play_style => persona(p,{play_style})} />
            <label>{p.seat} 号模型连接<select value={p.connection_id} onChange={e => update(p.seat, {connection_id:e.target.value})}><option value="">请选择连接</option>{p.connection_id && !c && <option value={p.connection_id}>原连接已删除或不可用（请重新选择）</option>}{connections.map(item => <option key={item.id} value={item.id}>{item.label} · {providers[item.provider]} · {item.model}</option>)}</select></label>
            {result?.thinking_mode === 'low' && <p className="helper">{result.provider === 'dashscope' ? '限额思考' : '低档思考'} · {thinkingLimitsText(result)}</p>}
            <details><summary>{p.seat} 号个性描述与玩法倾向</summary><label>{p.seat} 号个性描述<textarea maxLength={2000} rows={2} value={p.persona.description} onChange={e => persona(p, {label:'自定义', description:e.target.value})} /></label><div className="two-fields"><label>{p.seat} 号风险偏好<select value={p.persona.risk} onChange={e => persona(p, {risk:e.target.value as Persona['risk']})}><option value="cautious">谨慎</option><option value="balanced">均衡</option><option value="aggressive">激进</option></select></label><label>{p.seat} 号诈身份倾向<select value={p.persona.bluff} onChange={e => persona(p, {bluff:e.target.value as Persona['bluff']})}><option value="low">低</option><option value="medium">中</option><option value="high">高</option></select></label></div></details>
            {result?.error && <p className="helper error-text">{qualificationError(result.error)}</p>}
          </article>;
        })}</div></details>
        <label>默认推进模式<select value={plan.playback_mode} onChange={e => edit({...plan, playback_mode:e.target.value as TablePlan['playback_mode']})}><option value="manual">单步 · 每步后等我继续</option><option value="auto">自动推进</option></select></label>
        <p className="helper">模型、参数、密钥、规则或个性变化后重新检查配置即可，不考试。项目规则 Skill 会随每次决策发送给模型，不需要安装。</p>
        <div className="planner-actions"><button type="button" className="primary" onClick={() => {void operate(false);}}>检查开局配置 · 不调用模型</button><button type="button" className="secondary" onClick={() => {void operate(true);}}>仅保存配置（不开局）</button></div><p className="helper">检查后另行确认费用才开局。保存配置可选，不是开局前的必经按钮。</p>
      </fieldset>
    </>}
    <div className="planner-refresh"><button type="button" className="text-button" disabled={busy} onClick={() => setReload(n => n + 1)}>刷新连接与配置（保留修改）</button><button type="button" className="text-button" disabled={busy || invalidSession} onClick={() => {if (window.confirm('重新读取已保存的配置？当前未保存的修改会被替换。')) {restore.current = true; setNotice(''); setReload(n => n + 1);}}}>重新读取已保存配置</button></div>
  </div>;
}
