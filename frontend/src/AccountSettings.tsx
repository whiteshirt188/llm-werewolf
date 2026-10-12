import { useEffect, useRef, useState } from 'react';
import { ApiError, request } from './api.ts';
import { connectionBody, defaultGeneration, hunyuanModel, glmModel, qwenModel, supportsThinking, providers } from './account.ts';
import type { Account, Generation, Provider, SavedConnection } from './account.ts';

const signedOut: Account = {user: null, csrf_token: null};

export function AccountSettings({onAccountChange, openRequest = 0, onConfigurePlayers}: {onAccountChange?: (account: Account) => void; openRequest?: number; onConfigurePlayers?: () => void}) {
  const [account, setAccount] = useState<Account>(signedOut);
  const [open, setOpen] = useState(false), [statusError, setStatusError] = useState('');
  const load = async () => {
    try { setAccount(await request<Account>('/api/account/status')); setStatusError(''); }
    catch { setStatusError('账号服务暂时不可用，请打开后重试。'); }
  };
  useEffect(() => { void load(); }, []);
  useEffect(() => { if (openRequest > 0) setOpen(true); }, [openRequest]);
  useEffect(() => {onAccountChange?.(account);}, [account, onAccountChange]);
  return <><button className="secondary account-entry" title={statusError || undefined} onClick={() => { setOpen(true); }}>
    {account.user ? `${account.user.username} · 设置` : '登录 / 设置'}
  </button>{open && <AccountDialog initial={account} update={setAccount} configurePlayers={onConfigurePlayers} close={() => {setOpen(false); void load();}} />}</>;
}

function AccountDialog({initial, update, close, configurePlayers}: {initial: Account; update: (value: Account) => void; close: () => void; configurePlayers?: () => void}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [account, setAccount] = useState(initial), [ready, setReady] = useState(false);
  const [connections, setConnections] = useState<SavedConnection[]>([]);
  const [mode, setMode] = useState<'login' | 'register'>('login');
  const [username, setUsername] = useState(''), [password, setPassword] = useState(''), [repeat, setRepeat] = useState('');
  const [oldPassword, setOldPassword] = useState(''), [newPassword, setNewPassword] = useState('');
  const [error, setError] = useState(''), [notice, setNotice] = useState(''), [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState<SavedConnection | null>(null), [editorOpen, setEditorOpen] = useState(false);
  const [deleting, setDeleting] = useState<SavedConnection | null>(null);
  const apply = (value: Account) => { setAccount(value); update(value); };
  const resetPrivateUI = () => { setConnections([]); setEditing(null); setEditorOpen(false); setDeleting(null); setPassword(''); setRepeat(''); setOldPassword(''); setNewPassword(''); };
  useEffect(() => {
    const element = dialog.current; element?.showModal();
    let stopped = false;
    request<Account>('/api/account/status').then(result => {
      if (!stopped) { setAccount(result); update(result); setReady(true); }
    }).catch(() => { if (!stopped) { setError('账号服务暂时不可用，请关闭后重试。'); setReady(false); } });
    return () => { stopped = true; element?.close(); };
  }, []); // Refresh session/CSRF each time the settings dialog opens.
  useEffect(() => {
    if (!ready || !account.user) return;
    let stopped = false;
    request<{connections: SavedConnection[]}>('/api/account/connections').then(result => { if (!stopped) setConnections(result.connections); })
      .catch(err => { if (!stopped) { setError(err instanceof Error ? err.message : '读取设置失败'); if (err instanceof ApiError && err.status === 401) { resetPrivateUI(); apply(signedOut); } } });
    return () => { stopped = true; };
  }, [ready, account.user?.id]);
  const operation = async (job: () => Promise<void>) => {
    setBusy(true); setError(''); setNotice('');
    try { await job(); }
    catch (err) { setError(err instanceof Error ? err.message : '操作失败'); if (err instanceof ApiError && err.status === 401 && account.user) { resetPrivateUI(); apply(signedOut); } }
    finally { setBusy(false); }
  };
  const authenticated = <T,>(path: string, body: unknown) => request<T>(path, body, {'X-CSRF-Token': account.csrf_token ?? ''});
  const dismiss = () => { dialog.current?.close(); close(); };
  return <dialog ref={dialog} className="account-dialog" aria-labelledby="account-title" onCancel={event => { event.preventDefault(); dismiss(); }}>
    <div className="dialog-heading"><div><span className="eyebrow">LOCAL ACCOUNT</span><h2 id="account-title">{account.user ? '模型与账号设置' : '本机账号'}</h2></div><button className="secondary" autoFocus onClick={dismiss}>关闭设置</button></div>
    <p className="helper account-scope">仅保存在运行后端的这台电脑，不是云同步。登录管理配置；未登录仍可玩离线局。</p>
    {error && <p role="alert" className="error-text">{error}</p>}{notice && <p role="status" className="account-notice">{notice}</p>}
    {!ready ? <p>正在确认登录状态…</p> : !account.user ? <>
      <div className="account-tabs"><button className={mode === 'login' ? 'primary' : 'secondary'} onClick={() => { setMode('login'); setPassword(''); setRepeat(''); setError(''); }}>登录</button><button className={mode === 'register' ? 'primary' : 'secondary'} onClick={() => { setMode('register'); setPassword(''); setRepeat(''); setError(''); }}>创建本机账号</button></div>
      <form className="account-form" onSubmit={e => { e.preventDefault(); if (mode === 'register' && password !== repeat) { setError('两次密码不一致。'); return; }
        void operation(async () => {
          const result = await request<Account>(`/api/account/${mode}`, {username, password});
          resetPrivateUI(); apply(result); setNotice(mode === 'register' ? '账号已创建，现在可以保存模型连接。' : '已登录。');
        });
      }}><label>用户名<input required minLength={3} maxLength={32} pattern="[a-zA-Z0-9_-]+" autoComplete="username" value={username} onChange={e => setUsername(e.target.value)} /></label>
        <p className="helper">3–32 位英文、数字、下划线或连字符，不区分大小写。</p>
        <label>登录密码<input type="password" required minLength={10} maxLength={128} autoComplete={mode === 'register' ? 'new-password' : 'current-password'} value={password} onChange={e => setPassword(e.target.value)} /></label>
        {mode === 'register' && <label>确认密码<input type="password" required minLength={10} maxLength={128} autoComplete="new-password" value={repeat} onChange={e => setRepeat(e.target.value)} /></label>}
        <p className="helper">密码至少 10 个字符。首版没有邮件找回，请妥善保存。</p><button className="primary" disabled={busy}>{busy ? '处理中…' : mode === 'register' ? '创建并登录' : '登录本机账号'}</button>
      </form>
    </> : <>
      <div className="account-user"><span>已登录 · <b>{account.user.username}</b></span><button className="secondary" disabled={busy} onClick={() => { void operation(async () => { const result = await authenticated<Account>('/api/account/logout', {}); resetPrivateUI(); apply(result); setNotice('已退出账号；已有离线对局不受影响。'); }); }}>退出登录</button></div>
      <div className="section-heading"><h3>我的模型连接</h3><button className="primary" disabled={busy || editorOpen} onClick={() => { setEditing(null); setEditorOpen(true); setDeleting(null); }}>添加连接</button></div>
      <div className="account-next-step"><b>保存 API ≠ 开始游戏</b><p className="helper">把 API 分配给玩家 → 检查开局配置 → 确认费用后开局，不再进行资格考试。同一 API 可供多个独立席位使用；支持 DeepSeek、通义千问与硅基流动混元、GLM-5.3。</p>{configurePlayers ? <button className="primary" disabled={busy || editorOpen} onClick={() => {dismiss(); configurePlayers();}}>下一步：配置真实玩家 →</button> : <p className="helper">正在对局时不会跳走；这局结束后点击“开新局”，选择“真实模型对局”。</p>}</div>
      <div className="saved-connections">{connections.length ? connections.map(c => <article className="saved-connection" key={c.id}>
        <div><h3>{c.label}</h3><p>{providers[c.provider]} · {c.model}{supportsThinking(c.provider) ? ` · ${c.generation.thinking_mode === 'low' ? c.provider === 'dashscope' ? '限额思考' : '低档思考' : '思考关闭'}` : ''}</p><small>API Key {c.key_mask} · 已保存（不代表已联网验证）</small></div><div className="connection-actions"><button className="secondary" disabled={busy || editorOpen} onClick={() => { setEditing(c); setEditorOpen(true); setDeleting(null); }}>编辑</button><button className="text-button" disabled={busy || editorOpen} onClick={() => setDeleting(c)}>删除</button></div>
      </article>) : <p className="empty">还没有保存连接。填写一次，下次登录即可继续使用。</p>}</div>
      {deleting && <section className="account-confirm"><p>删除“{deleting.label}”？该配置及保存的密钥会从账号库移除，不影响离线对局。</p><button className="secondary" disabled={busy} onClick={() => setDeleting(null)}>取消删除</button><button className="primary" disabled={busy} onClick={() => { void operation(async () => { await authenticated(`/api/account/connections/${deleting.id}/delete`, {confirm: true}); setConnections(list => list.filter(c => c.id !== deleting.id)); setDeleting(null); setNotice('连接已删除。'); }); }}>确认删除连接</button></section>}
      {editorOpen && <ConnectionEditor key={editing?.id ?? 'new'} connection={editing} busy={busy} cancel={() => { setEditorOpen(false); setEditing(null); }} save={body => { void operation(async () => {
        const saved = await authenticated<SavedConnection>(`/api/account/connections${editing ? `/${editing.id}` : ''}`, body);
        setConnections(list => editing ? list.map(c => c.id === saved.id ? saved : c) : [...list, saved]);
        setEditorOpen(false); setEditing(null); setNotice('连接已保存并加密。没有发起模型调用。');
      }); }} />}
      <details className="account-password"><summary>修改登录密码</summary><form className="account-form" onSubmit={e => { e.preventDefault(); void operation(async () => {
        const result = await authenticated<Account>('/api/account/password', {old_password: oldPassword, new_password: newPassword});
        apply(result); setOldPassword(''); setNewPassword(''); setNotice('密码已修改，其他浏览器的账号登录已失效；保存的模型连接不变。');
      }); }}><label>当前密码<input type="password" required minLength={10} maxLength={128} autoComplete="current-password" value={oldPassword} onChange={e => setOldPassword(e.target.value)} /></label><label>新密码<input type="password" required minLength={10} maxLength={128} autoComplete="new-password" value={newPassword} onChange={e => setNewPassword(e.target.value)} /></label><button className="secondary" disabled={busy}>保存新密码</button></form></details>
      <p className="helper">API Key 保存后不再返回完整内容，不进入对局日志或复盘。请勿公开分享本机账号库和加密密钥文件；本机登录不是对同一 macOS 用户的安全隔离。</p>
    </>}
  </dialog>;
}

function ConnectionEditor({connection, busy, cancel, save}: {connection: SavedConnection | null; busy: boolean; cancel: () => void; save: (body: unknown) => void}) {
  const [label, setLabel] = useState(connection?.label ?? ''), [provider, setProvider] = useState<Provider>(connection?.provider ?? 'deepseek');
  const [model, setModel] = useState(connection?.model ?? ''), [key, setKey] = useState('');
  const [generation, setGeneration] = useState<Generation>({...defaultGeneration(), ...connection?.generation});
  const thinking = supportsThinking(provider) && generation.thinking_mode === 'low';
  const [error, setError] = useState('');
  const number = (name: keyof Generation, value: string) => setGeneration(g => ({...g, [name]: value === '' ? name === 'reasoning_budget' ? null : NaN : Number(value)}));
  return <form className="account-form connection-editor" onSubmit={e => { e.preventDefault(); setError('');
    if (connection && provider !== connection.provider && !key) { setError('更换供应商时请重新填写 API Key。'); return; }
    try { save(connectionBody(label, provider, model, generation, key, Boolean(connection))); }
    catch (err) { setError(err instanceof Error ? err.message : '参数不正确'); }
  }}><h3>{connection ? '编辑连接' : '添加模型连接'}</h3><div className="two-fields"><label>连接名称<input required maxLength={64} value={label} onChange={e => setLabel(e.target.value)} placeholder="例如：我的通义主力" /></label><label>供应商<select value={provider} onChange={e => { const next = e.target.value as Provider; setProvider(next); setKey(''); if (next === 'dashscope') setModel(qwenModel); else if (provider === 'dashscope') setModel(''); if (!supportsThinking(next)) setGeneration(g => ({...g, thinking_mode:'disabled'})); }}>
    {Object.entries(providers).map(([value, name]) => <option value={value} key={value}>{name}</option>)}
  </select></label></div><label>模型 ID<input required maxLength={128} value={model} onChange={e => setModel(e.target.value)} placeholder="填写供应商提供的准确模型 ID" autoComplete="off" /></label>
    {provider === 'siliconflow' && <p className="helper">支持混元 {hunyuanModel} 和 GLM-5.3 {glmModel}。<button type="button" className="text-button" onClick={() => setModel(hunyuanModel)}>填入混元模型 ID</button><button type="button" className="text-button" onClick={() => setModel(glmModel)}>填入 GLM-5.3 模型 ID</button> 请使用硅基流动 API Key；保存不调用模型。GLM-5.3 始终开启思考，实际用量会计入对局记录。</p>}
    {provider === 'dashscope' && <p className="helper">使用阿里云百炼北京地域的 API Key。<button type="button" className="text-button" onClick={() => setModel(qwenModel)}>填入千问 Plus 模型 ID</button> 默认 {qwenModel}，也可用已接入的 Qwen3.5/3.6/3.7 Plus、Flash 或 Qwen3.7/3.8 Max、Qwen3.8 Flash。保存和检查配置不调用模型。</p>}
    <label>API Key{connection ? '（留空保留原密钥）' : ''}<input type="password" required={!connection || provider !== connection.provider} maxLength={2048} autoComplete="off" spellCheck={false} value={key} onChange={e => setKey(e.target.value)} placeholder={connection ? '不返回原密钥；需要时填写新密钥' : '仅发送给本机后端，加密保存'} /></label>
    {supportsThinking(provider) && <><label>思考模式<select value={generation.thinking_mode ?? 'disabled'} onChange={e => setGeneration(g => ({...g, thinking_mode:e.target.value as Generation['thinking_mode']}))}><option value="disabled">关闭 · 更快</option><option value="low">{provider === 'dashscope' ? '开启 · 限额思考' : '低档思考 · 试用'}</option></select></label><p className="helper">{provider === 'dashscope' ? '千问思考额度与 DeepSeek 相同，建议使用 qwen3.6-plus；qwen-plus / qwen-flash 仅支持关闭思考。' : '低档思考用于 deepseek-flash / deepseek-v4-pro。'}开启可能增加等待和用量。保存后重新检查配置、开新局生效。</p></>}
    <details><summary>生成参数</summary><div className="two-fields account-generation">{thinking ? <label>首轮普通席基准额度（tokens）<input type="number" required min={1024} max={16384} step={1} value={Number.isNaN(generation.thinking_output_tokens) ? '' : generation.thinking_output_tokens ?? 4096} onChange={e => number('thinking_output_tokens', e.target.value)} /></label> : <><label>温度<input type="number" required min={0} max={2} step="0.1" value={Number.isNaN(generation.temperature) ? '' : generation.temperature} onChange={e => number('temperature', e.target.value)} /></label><label>普通输出上限<input type="number" required min={1} step={1} value={Number.isNaN(generation.max_output_tokens) ? '' : generation.max_output_tokens} onChange={e => number('max_output_tokens', e.target.value)} /></label><label>目标选择输出上限<input type="number" required min={1} step={1} value={Number.isNaN(generation.target_output_tokens) ? '' : generation.target_output_tokens} onChange={e => number('target_output_tokens', e.target.value)} /></label><label>推理预算（可选）<input type="number" min={1} step={1} value={generation.reasoning_budget ?? ''} onChange={e => number('reasoning_budget', e.target.value)} /></label></>}</div><p className="helper">{thinking ? '新局神职额度 ×2，第二晚起再 ×2，合计最高 16384 tokens；思考与回答共享额度，拿牌准备使用基准额度。更高额度可能增加费用，发言字数限制保留，温度不生效。' : '这些是保存的请求配置，不代表所有模型支持相同参数；接入时由适配器校验能力。本轮不验证模型 ID。'}</p></details>
    {error && <p className="error-text" role="alert">{error}</p>}<div className="account-actions"><button type="button" className="secondary" disabled={busy} onClick={cancel}>取消编辑</button><button className="primary" disabled={busy}>{busy ? '保存中…' : '保存连接'}</button></div>
  </form>;
}
