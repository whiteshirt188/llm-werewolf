import { useEffect, useRef, useState } from 'react';
import type { Persona } from './types.ts';
import { defaultPersona, presets } from './game.ts';
import { ApiError, request } from './api.ts';
import type { SavedConnection } from './account.ts';
import { qualificationError, qualificationStartUncertain, qualificationStatus, thinkingNotice } from './qualification.ts';
import type { QualificationPreview, QualificationReport } from './qualification.ts';
import { sixRoster, ruleConfig } from './roster.ts';
import type { Roster } from './roster.ts';
import { rosterText } from './game.ts';
import { RosterSettings } from './RosterSettings.tsx';
import { StrategySelect } from './StrategySelect.tsx';

export function QualificationPanel({connection, csrf, close, initialPersona, roster}: {connection: SavedConnection; csrf: string; close: () => void; initialPersona?: Persona; roster?: Roster}) {
  const [selectedRoster, setSelectedRoster] = useState<Roster>(sixRoster);
  const examRoster = roster ?? selectedRoster;
  const rulesKey = JSON.stringify(examRoster);
  const [persona, setPersona] = useState<Persona>(() => initialPersona ? {...initialPersona} : defaultPersona());
  const [preview, setPreview] = useState<QualificationPreview | null>(null), [report, setReport] = useState<QualificationReport | null>(null);
  const [confirm, setConfirm] = useState(false), [force, setForce] = useState(false), [busy, setBusy] = useState(false);
  const [error, setError] = useState(''), [loadVersion, setLoadVersion] = useState(0);
  const pending = useRef<Record<string, unknown> | null>(null);
  const [unconfirmed, setUnconfirmed] = useState(false);
  const post = <T,>(path: string, body: unknown) => request<T>(path, body, {'X-CSRF-Token': csrf});
  useEffect(() => {
    let stopped = false;
    setPreview(null); setReport(null); setConfirm(false); setError('');
    post<QualificationPreview>('/api/account/qualifications/preview', {connection_id: connection.id, persona, roster:JSON.parse(rulesKey)})
      .then(value => { if (!stopped) { setPreview(value); setReport(value.latest_report); } })
      .catch(err => { if (!stopped) setError(err instanceof Error ? err.message : '预览失败'); });
    return () => { stopped = true; };
  }, [connection.id, persona, csrf, loadVersion, rulesKey]);
  useEffect(() => {
    if (report?.status !== 'running') return;
    let stopped = false, timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const value = await request<QualificationReport>(`/api/account/qualifications/${report.id}`);
        if (!stopped) { setReport(value); setError(''); if (value.status === 'running') timer = setTimeout(poll, 1000); }
      } catch { if (!stopped) { setError('报告暂时无法读取。查询不会启动新调用；可重新预览恢复。'); } }
    };
    timer = setTimeout(poll, 500);
    return () => { stopped = true; clearTimeout(timer); };
  }, [report?.id, report?.status]);
  const start = async () => {
    if (!preview || busy) return;
    const body = pending.current ?? {connection_id: connection.id, persona, roster:JSON.parse(rulesKey), operation_id: crypto.randomUUID(),
      expected_version: preview.configuration_version, confirm_cost: true, force_retest: force};
    pending.current = body; setBusy(true); setError('');
    try {
      const value = await post<QualificationReport>('/api/account/qualifications', body);
      setReport(value); pending.current = null; setUnconfirmed(false); setConfirm(false);
    } catch (err) {
      if (err instanceof ApiError && !qualificationStartUncertain(err.status)) { pending.current = null; setUnconfirmed(false); setError(err.message); }
      else { setUnconfirmed(true); setError('启动结果尚未确认。重试会使用原操作编号，不会重复启动；不要另开一次考试。'); }
    } finally { setBusy(false); }
  };
  const running = report?.status === 'running';
  return <section className="qualification-panel" aria-label="上桌资格测试">
    <div className="section-heading"><div><span className="eyebrow">ADMISSION TEST</span><h3>上桌资格 · {connection.label}</h3></div><button className="text-button" onClick={close}>收起报告</button></div>
    {!roster && <fieldset disabled={busy || running || unconfirmed}><legend className="sr-only">考试阵容</legend><RosterSettings value={examRoster} onChange={setSelectedRoster} /></fieldset>}
    <p className="roster-summary">本次考试阵容：{examRoster.players} 人 · {rosterText(ruleConfig(examRoster))}</p>
    <p className="helper">资格绑定此人数与阵容。开局配置不同不能复用；相同阵容、API 和个性的多个席位共享结果。</p>
    <p className="helper">基础 6 个规则题 + 所选新增神职各 1 题，规则题必须全对；6 个推理题至少对 5 个。每题独立上下文、程序判分；有效资格不自动开局，需另行确认对局费用。</p>
    {preview?.simulated_transport && <p className="account-notice">隔离模拟接口：不调用供应商、不产生费用、不授予真实资格。</p>}
    <fieldset disabled={busy || running || unconfirmed} className="qualification-persona"><legend>考试个性（需要与将来参赛个性一致）</legend>
      <StrategySelect label="资格决策风格" value={persona.strategy} onChange={strategy => setPersona(p => ({...p,strategy}))} />
      <label>资格个性<select value={persona.label} onChange={e => setPersona(p => ({...p, label:e.target.value, description:presets[e.target.value] ?? p.description}))}>
        {[...Object.keys(presets), '自定义'].map(name => <option key={name}>{name}</option>)}
      </select></label><label>资格个性描述<textarea rows={2} maxLength={2000} value={persona.description} onChange={e => setPersona(p => ({...p,label:'自定义',description:e.target.value}))} /></label>
      <div className="two-fields"><label>资格风险偏好<select value={persona.risk} onChange={e => setPersona(p => ({...p,risk:e.target.value as Persona['risk']}))}><option value="cautious">谨慎</option><option value="balanced">均衡</option><option value="aggressive">激进</option></select></label><label>资格诈身份倾向<select value={persona.bluff} onChange={e => setPersona(p => ({...p,bluff:e.target.value as Persona['bluff']}))}><option value="low">低</option><option value="medium">中</option><option value="high">高</option></select></label></div>
    </fieldset>
    {error && <p className="error-text" role="alert">{error}</p>}
    {preview?.unsupported && <p className="error-text">{qualificationError(preview.unsupported)}</p>}
    {report && <div className="qualification-result"><h3>{qualificationStatus(report)}</h3><p>规则 {report.rule_passed} / {report.rule_total ?? 6} · 推理 {report.logic_passed} / {report.logic_total ?? 6} · 已完成 {report.results.length} / {report.question_total ?? 12} 题</p>
      {report.error && <p className="error-text">{qualificationError(report.error)}</p>}
      <p className="helper">{report.simulated ? '模拟' : '供应商'}调用 {report.usage.calls} 次（含重试）。已知输入 {report.usage.known_input_tokens} / 输出 {report.usage.known_output_tokens} tokens；{report.usage.unknown_usage_calls} 次用量未知。</p>
      <p className="helper">已知缓存输入 {report.usage.known_cached_input_tokens}、推理 {report.usage.known_reasoning_tokens} 为子项，不另加到总量；分别有 {report.usage.unknown_cached_usage_calls} / {report.usage.unknown_reasoning_usage_calls} 次未报告。未配置价格，不估算金额；账单以供应商为准。</p>
      <ol className="qualification-questions">{report.results.map(result => <li key={result.id}><span>{result.passed ? '✓' : '×'}</span><b>{result.title}</b><small>{result.passed ? '通过' : qualificationError(result.error)}</small></li>)}</ol>
      <details><summary>逐次调用记录 · {report.attempts.length} 次</summary><div className="usage-table"><table><thead><tr><th>题目</th><th>尝试</th><th>结果</th><th>输入 / 输出 token</th></tr></thead><tbody>{report.attempts.map(a => <tr key={a.id}><td>{a.question_id}</td><td>{a.retry + 1}</td><td>{a.error ? qualificationError(a.error) : a.status}</td><td>{a.usage.input_tokens ?? '未知'} / {a.usage.output_tokens ?? '未知'}</td></tr>)}</tbody></table></div></details>
    </div>}
    {!running && preview && !preview.unsupported && <div className="qualification-consent">
      {report?.qualified && <label className="check-label"><input type="checkbox" checked={force} disabled={busy || unconfirmed} onChange={e => {setForce(e.target.checked); setConfirm(false);}} />强制重新考试（会再次调用供应商）</label>}
      {(!report?.qualified || force) && <><p>通常 {preview.normal_calls} 次调用，临时故障最多重试两次，总计最多 {preview.maximum_calls} 次。鉴权失败立即停止；错误答案不纠错重答。{thinkingNotice(preview.thinking_mode)}</p><label className="check-label"><input type="checkbox" checked={confirm} disabled={busy || unconfirmed} onChange={e => setConfirm(e.target.checked)} />{preview.simulated_transport ? '我确认运行本次隔离模拟考试' : '我确认发起本次真实考试，知道可能产生供应商费用'}</label></>}
      <button className="primary" disabled={busy || (!unconfirmed && (!confirm || Boolean(report?.qualified && !force)))} onClick={() => {void start();}}>{busy ? '确认启动中…' : unconfirmed ? '重试确认原启动请求' : report?.qualified && !force ? '已有有效资格，无需重复调用' : preview.simulated_transport ? '开始模拟资格测试' : '开始真实资格测试'}</button>
    </div>}
    {running && <button className="secondary" disabled={busy} onClick={async () => {setBusy(true); try {setReport(await post<QualificationReport>(`/api/account/qualifications/${report.id}/cancel`, {}));} catch (err) {setError(err instanceof Error ? err.message : '取消状态未确认，请重新读取报告');} finally {setBusy(false);}}}>取消后续测试</button>}
    <button className="text-button" disabled={busy || unconfirmed} onClick={() => setLoadVersion(v => v + 1)}>重新预览 / 读取报告（不调用模型）</button>
    <p className="helper">收起报告或关闭弹窗不会停止考试；请用“取消后续测试”。退出账号会取消后续测试，已发出的调用可能计费。模型、生成参数、个性、规则或 Skill 改变后需匹配新资格；同连接同配置的多个席位共享结果。刷新和查询不会启动考试。</p>
  </section>;
}
