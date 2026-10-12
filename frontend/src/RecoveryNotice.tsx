import type {DecisionRecovery, RecoveryStrategy} from './types.ts';
import {recoveryExplanation} from './recovery.ts';

export function RecoveryNotice({recovery, busy, resume}:{recovery:DecisionRecovery; busy:boolean; resume:(strategy:RecoveryStrategy)=>void}) {
  return <div className="notice recovery-notice"><b>这一步可以恢复</b><p>{recoveryExplanation(recovery)}</p>
    <p className="helper">只改变本次请求；后续继续使用原配置。增加额度可能更慢、费用更高。关闭思考后直接生成行动。</p>
    <div className="recovery-actions">
      {recovery.increased_limit !== null && <button className="secondary" disabled={busy} onClick={()=>resume('more_tokens')}>本次增加额度继续 · {recovery.increased_limit}</button>}
      <button className="primary" disabled={busy} onClick={()=>resume('without_thinking')}>本次关闭思考继续</button>
    </div></div>;
}
