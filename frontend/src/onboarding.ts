import type { SavedConnection } from './account.ts';
import { playableConnection } from './account.ts';
import type { Admission, TablePlan } from './table-plan.ts';
import { admissionReady, seatReady } from './table-plan.ts';

type Guide = {step: 1 | 2 | 3 | 4; title: string; detail: string;
  action: 'settings' | 'connections' | 'preflight' | 'consent' | 'existing' | 'retry' | null; label: string};

// Presentation only. Server admission and the separate fee confirmation remain authoritative.
export function plannerGuide(plan: TablePlan, connections: SavedConnection[], admission: Admission | null,
  ready: boolean, existing: boolean, uncertain: boolean): Guide {
  const count = (plan.roster?.players ?? 6) - 1, label = count === 5 ? '五' : String(count);
  if (uncertain) return {step:4, title:'先确认刚才的开局结果', detail:'不要另开一局。用原请求重试确认，避免重复开局；已经发出的请求可能计费。', action:'retry', label:'重试确认原开局请求'};
  if (existing) return {step:4, title:'你已经有一局真实对局', detail:'先回到已有牌桌。读取不调用模型；手动恢复运行可能产生费用。', action:'existing', label:'进入已有真实对局'};
  if (!ready) return {step:1, title:'正在读取你的 API 和席位配置', detail:'读取和刷新不会调用模型。若读取失败，请用下方刷新按钮重试。', action:null, label:''};
  if (!connections.some(playableConnection)) return {step:1, title:'先保存一个可参赛 API', detail:'打开账号设置，保存 DeepSeek、通义千问官方连接，或硅基流动混元 / GLM-5.3。保存 API 本身不收费，也不调用模型。', action:'settings', label:'打开设置 · 保存 API'};
  const missing = plan.ai_profiles.filter(p => !connections.some(c => c.id === p.connection_id && playableConnection(c)));
  if (missing.length) return {step:2, title:`把 API 分配给${label}个 AI 玩家`, detail:`${missing.map(p => `${p.seat} 号`).join('、')}还没选可参赛的连接。下方选已保存的支持模型，再点“应用到${label}个 AI”；也可分别配置。`, action:'connections', label:'去选择已保存的 API'};
  if (!admission) return {step:3, title:`下一步：检查${label}个玩家的开局配置`, detail:'资格测试已取消。这里只检查连接归属、支持的模型和生成参数，不调用模型，也不验证密钥余额或模型实际效果。', action:'preflight', label:'检查开局配置 · 不调用模型'};
  if (!admissionReady(admission)) {
    const blocked = admission.seats.filter(s => !seatReady(s)).map(s => `${s.seat} 号`).join('、');
    return {step:3, title:blocked ? `${blocked}的配置需要修改` : '当前配置还不能开局', detail:'展开玩家设置查看连接或参数原因，修改后重新检查配置。不需要考试；检查不联网、不产生供应商费用。', action:'connections', label:'查看并修改模型连接'};
  }
  return {step:4, title:`${label}个玩家配置已就绪，只差你确认开局`, detail:'保存和检查配置都不是开局。只有下方勾选费用确认并点击“开始真实模型对局”，才会发起游戏调用；单步模式也可能预取一个行动。', action:'consent', label:'查看开局费用确认'};
}
