import {useEffect, useRef, useState} from 'react';
import {ApiError, request} from './api.ts';
import {planBody} from './table-plan.ts';
import type {TablePlan} from './table-plan.ts';
import {qualificationError, qualificationStartUncertain, thinkingNotice} from './qualification.ts';
import {rosterText} from './game.ts';
import {ruleConfig} from './roster.ts';
import type {Roster} from './roster.ts';

type Group = {connection_id: string; seats: number[]; cached: boolean; unsupported: string | null; status: string; report_id: string | null;
  thinking_mode?: 'disabled' | 'requested_disabled';
  exam?: {status:string; qualified:boolean; simulated:boolean; error:string | null; completed:number; question_total:number; rule_passed:number; rule_total:number; logic_passed:number}};
type Preview = {version:string; groups:Group[]; normal_calls:number; maximum_calls:number; blocked:boolean; simulated_transport?:boolean};
type Batch = Preview & {id:string; status:string; error:string | null; roster?:Roster;
  usage?: {calls:number; known_input_tokens:number; known_output_tokens:number; unknown_usage_calls:number}};

export function QualificationBatch({plan,seed,csrf,disabled,onBusy,onFinished}: {plan:TablePlan; seed:string; csrf:string; disabled:boolean; onBusy:(v:boolean)=>void; onFinished:()=>void}) {
  const [preview,setPreview] = useState<Preview | null>(null), [batch,setBatch] = useState<Batch | null>(null);
  const [confirm,setConfirm] = useState(false), [loading,setLoading] = useState(false), [uncertain,setUncertain] = useState(false), [error,setError] = useState('');
  const pending = useRef<Record<string,unknown> | null>(null), snapshot = useRef<TablePlan | null>(null);
  const callbacks = useRef({onBusy,onFinished}); callbacks.current = {onBusy,onFinished};
  const running = batch?.status === 'running', locked = Boolean(running || loading || uncertain);
  const post = <T,>(path:string,body:unknown) => request<T>(path,body,{'X-CSRF-Token':csrf});
  useEffect(() => {callbacks.current.onBusy(locked);}, [locked]);
  useEffect(() => {setPreview(null);setConfirm(false);}, [JSON.stringify(plan),seed]);
  useEffect(() => {
    let stopped = false;
    request<{batch:Batch | null}>('/api/account/qualification-batches/current').then(r => {if (!stopped) setBatch(r.batch);})
      .catch(() => {if (!stopped) setError('暂时无法读取一键测试记录；可点击“读取测试进度”重试。');});
    return () => {stopped=true;};
  }, [csrf]);
  useEffect(() => {
    if (!running || !batch) return;
    let stopped = false, timer:ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const result = await request<Batch>(`/api/account/qualification-batches/${batch.id}`);
        if (stopped) return;
        setBatch(result);setError('');
        if (result.status !== 'running') {callbacks.current.onFinished();return;}
      } catch {if (!stopped) setError('进度暂时无法读取，正在重试查询；不会重复发起测试。');}
      if (!stopped) timer=setTimeout(poll,1000);
    };
    timer=setTimeout(poll,400);
    return () => {stopped=true;clearTimeout(timer);};
  }, [batch?.id,running]);
  const prepare = async () => {
    setLoading(true);setError('');setConfirm(false);
    try {snapshot.current=planBody(plan,seed);setPreview(await post<Preview>('/api/account/qualification-batches/preview',snapshot.current));}
    catch (e) {setError(e instanceof Error ? e.message : '预览失败');}
    finally {setLoading(false);}
  };
  const start = async () => {
    if ((!confirm && !uncertain) || !preview || !snapshot.current || loading) return;
    pending.current ??= {plan:snapshot.current,operation_id:crypto.randomUUID(),expected_version:preview.version,confirm_cost:true};
    setLoading(true);setError('');
    try {
      const result=await post<Batch>('/api/account/qualification-batches',pending.current);
      setBatch(result);pending.current=null;setUncertain(false);setPreview(null);setConfirm(false);
      if (result.status !== 'running') callbacks.current.onFinished();
    } catch (e) {
      if (e instanceof ApiError && !qualificationStartUncertain(e.status)) {pending.current=null;setUncertain(false);setPreview(null);setError(e.message);}
      else {setUncertain(true);setError('启动结果未确认，请重试原请求。不会重复测试；刷新也能读取服务器进度。');}
    } finally {setLoading(false);}
  };
  const names:Record<string,string>={pending:'等待测试',cached:'已有资格 · 本轮跳过',running:'正在测试',passed:'题目通过',failed:'未通过',cancelled:'已取消',incomplete:'未完成'};
  return <section className="qualification-panel batch-panel" aria-label="一键资格测试">
    <div className="section-heading"><div><span className="eyebrow">ONE CLICK · ADMISSION</span><h3>一键测试所有未合格配置</h3></div><button className="primary" disabled={disabled || locked} onClick={() => {void prepare();}}>一键测试 · 先预览</button></div>
    <p className="helper">相同 API、阵容和有效个性合并为一组，已有有效资格跳过。一次确认后逐组执行，无需逐席点击；不会自动开局。</p>
    {(preview?.simulated_transport || batch?.simulated_transport) && <p className="account-notice">隔离模拟接口：不调用供应商，不产生费用，不授予真实资格。</p>}
    {preview?.groups.some(g => g.thinking_mode === 'requested_disabled' && !g.cached) && <p className="helper">{thinkingNotice('requested_disabled')}</p>}
    {error && <p className="error-text" role="alert">{error}</p>}
    {preview && <div className="qualification-consent"><h3>{preview.groups.filter(g=>!g.cached).length} 组需测试 · {preview.groups.filter(g=>g.cached).length} 组跳过</h3><p>通常 {preview.normal_calls} 次，临时故障含重试最多 {preview.maximum_calls} 次调用。金额未配置，账单以供应商为准。鉴权、配置及网络故障未恢复时停止队列；答错不会自动重考。</p>
      {preview.blocked ? <p className="error-text">有不支持的配置，请先修改：{preview.groups.filter(g=>g.unsupported).map(g=>`${g.seats.join('、')} 号：${qualificationError(g.unsupported)}`).join('；')}</p> : preview.normal_calls === 0 ? <p role="status">全部已有有效资格，无需调用模型。点击“资格预检”即可准备开局。</p> : <><label className="check-label"><input type="checkbox" checked={confirm} disabled={loading || uncertain} onChange={e=>setConfirm(e.target.checked)} />我确认本次全部配置的资格测试，知道可能产生供应商费用</label><button className="primary" disabled={loading || (!confirm && !uncertain)} onClick={()=>{void start();}}>{uncertain ? '重试确认原启动请求' : '开始一键测试'}</button></>}
    </div>}
    {batch && <div className="qualification-result" aria-live="polite"><h3>{running ? '一键测试进行中' : batch.status === 'completed' ? '队列已完成 · 请查看各组资格' : names[batch.status] ?? '测试中断'}</h3>{batch.roster && <p className="helper">本报告阵容：{batch.roster.players} 人 · {rosterText(ruleConfig(batch.roster))}。修改表单后需重新预览，不代表新配置资格。</p>}<p>已处理 {batch.groups.filter(g=>!['pending','running'].includes(g.status)).length} / {batch.groups.length} 组</p><ol className="qualification-questions">{batch.groups.map((g,i)=><li key={i}><span>{g.exam?.qualified ? '✓' : g.status === 'running' ? '…' : '·'}</span><b>{g.seats.join('、')} 号 · {g.exam?.qualified ? '资格有效' : names[g.status] ?? g.status}</b><small>{g.exam ? `${g.exam.completed} / ${g.exam.question_total} 题 · 规则 ${g.exam.rule_passed}/${g.exam.rule_total} · 推理 ${g.exam.logic_passed}/6 · ${g.exam.qualified ? '通过' : g.exam.status === 'passed' ? g.exam.simulated ? '模拟通过不授予资格' : '历史通过 · 当前资格无效' : qualificationError(g.exam.error)}` : g.cached ? '不重复调用' : '独立上下文考试'}</small></li>)}</ol>
      {batch.error && <p className="error-text">{qualificationError(batch.error)}</p>}
      {batch.usage && <p className="helper">本队列调用 {batch.usage.calls} 次（不含历史缓存考试）；已知输入 {batch.usage.known_input_tokens} / 输出 {batch.usage.known_output_tokens} tokens；{batch.usage.unknown_usage_calls} 次用量未知。</p>}
      {running && <button className="secondary" disabled={loading} onClick={async()=>{setLoading(true);try {setBatch(await post<Batch>(`/api/account/qualification-batches/${batch.id}/cancel`,{}));callbacks.current.onFinished();}catch {setError('取消结果未确认，请读取进度重试。');}finally {setLoading(false);}}}>取消整个后续队列</button>}
    </div>}
    <button className="text-button" disabled={loading} onClick={async()=>{try {setBatch((await request<{batch:Batch | null}>('/api/account/qualification-batches/current')).batch);setError('');}catch {setError('读取失败，请稍后重试。');}}}>读取测试进度（不调用模型）</button>
    <p className="helper">刷新或关闭页面不停止服务器队列；退出账号会取消。服务重启中断后不自动续测，已发出的调用可能计费。最终准入须重新预检当前配置。</p>
  </section>;
}
