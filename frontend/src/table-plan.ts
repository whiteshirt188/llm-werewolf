import type { Persona, PlayStyle } from './types.ts';
import { defaultPersona, defaultPlayStyles } from './game.ts';
import { cleanRoster, seatNumbers, sixRoster } from './roster.ts';
import type { Roster } from './roster.ts';

export type PlanSeat = {seat: number; nickname: string; connection_id: string; persona: Persona};
export type TablePlan = {roster?: Roster; human_seat: number; seed: number | null; playback_mode: 'manual' | 'auto'; ai_profiles: PlanSeat[]};
export type SavedPlan = {version: string | null; plan: TablePlan | null};
export type ThinkingLimits = {opening:number; first_regular:number; first_god:number; later_regular:number; later_god:number};
export type AdmissionSeat = {seat: number; nickname: string; connection_id: string; connection_label: string | null;
  provider: string | null; model: string | null; qualified: boolean; status: string; error: string | null;
  report_id: string | null; configuration_version: string | null; ready?: boolean;
  thinking_mode?: 'disabled' | 'low'; thinking_output_tokens?: number | null; thinking_limits?: ThinkingLimits | null};
export type Admission = {roster?: Roster; plan_fingerprint: string; seats: AdmissionSeat[]; all_qualified: boolean;
  unique_connections: number; qualification_configurations: number; model_calls: 0; game_start_enabled: boolean;
  qualification_required?: boolean; all_ready?: boolean};
export const seatReady = (seat?: AdmissionSeat) => seat?.ready ?? seat?.qualified ?? false;
export function thinkingLimitsText(seat:AdmissionSeat):string {
  const limits=seat.thinking_limits;
  if (!limits) return `思考与回答合计最多 ${seat.thinking_output_tokens} tokens`;
  return `首轮普通席 ${limits.first_regular} / 神职 ${limits.first_god}；第二晚起普通席 ${limits.later_regular} / 神职 ${limits.later_god} tokens（思考与回答合计）。拿牌准备 ${limits.opening}。`;
}
export const admissionReady = (a: Admission | null) => Boolean(a?.game_start_enabled &&
  (a.qualification_required === false ? a.all_ready && a.seats.every(s => s.ready === true) : a.all_qualified));
export const newPlanSeat = (seat: number, connection = '', style: PlayStyle = 'serious'): PlanSeat => ({seat, nickname: `玩家 ${seat}`, connection_id: connection, persona: defaultPersona(style)});
export const emptyPlan = (): TablePlan => ({human_seat: 1, seed: null, playback_mode: 'manual', ai_profiles: [2,3,4,5,6].map((s,i) => newPlanSeat(s, '', defaultPlayStyles[i]))});
export function changeRoster(plan: TablePlan, roster: Roster, connection = ''): TablePlan {
  const human = Math.min(plan.human_seat, roster.players);
  return {...plan, roster:{...roster}, human_seat:human,
    ai_profiles:seatNumbers(roster.players).filter(s => s !== human).map((s,i) => {
      const old = plan.ai_profiles.find(p => p.seat === s);
      return old ? {...old, persona:{...old.persona}} : newPlanSeat(s, connection, defaultPlayStyles[i]);
    })};
}
export function changeHuman(plan: TablePlan, seat: number, connection = ''): TablePlan {
  if (!Number.isInteger(seat) || seat < 1 || seat > (plan.roster?.players ?? 6)) throw new Error('席位超出所选人数。');
  if (seat === plan.human_seat) return plan;
  const index = seatNumbers(plan.roster?.players ?? 6).filter(s => s !== seat).indexOf(plan.human_seat);
  return {...plan, human_seat: seat, ai_profiles: [...plan.ai_profiles.filter(p => p.seat !== seat), newPlanSeat(plan.human_seat, connection, defaultPlayStyles[index])].sort((a,b) => a.seat - b.seat)};
}
export function applyConnection(plan: TablePlan, id: string): TablePlan {
  return {...plan, ai_profiles: plan.ai_profiles.map(p => ({...p, connection_id: id, persona: {...p.persona}}))};
}
export function planBody(plan: TablePlan, seed: string): TablePlan {
  if (seed && (!/^-?\d+$/.test(seed) || !Number.isSafeInteger(Number(seed)))) throw new Error('种子需为安全范围内的整数，留空可随机。');
  const roster = cleanRoster(plan.roster ?? sixRoster()), players = roster.players;
  if (plan.ai_profiles.length !== players - 1 || new Set(plan.ai_profiles.map(p => p.seat)).size !== players - 1 ||
    plan.ai_profiles.some(p => !Number.isInteger(p.seat) || p.seat < 1 || p.seat > players || p.seat === plan.human_seat) ||
    !Number.isInteger(plan.human_seat) || plan.human_seat < 1 || plan.human_seat > players) throw new Error('需要为每个非人类席位配置一个不同 AI。');
  if (plan.ai_profiles.some(p => !p.connection_id)) throw new Error('请为每个 AI 选择已保存的连接。');
  // Whitelist references, not connection metadata, keys or qualification claims.
  return {...(plan.roster ? {roster} : {}), human_seat: plan.human_seat, seed: seed ? Number(seed) : null, playback_mode: plan.playback_mode,
    ai_profiles: plan.ai_profiles.map(p => ({seat:p.seat, nickname:p.nickname, connection_id:p.connection_id,
      persona:{label:p.persona.label, description:p.persona.description, risk:p.persona.risk, bluff:p.persona.bluff, strategy:p.persona.strategy ?? 'balanced', play_style:p.persona.play_style ?? 'serious'}}))};
}
export function diversifyPlayStyles(plan: TablePlan): TablePlan {
  const seats = plan.ai_profiles.map(p => p.seat).sort((a,b) => a-b);
  return {...plan, ai_profiles:plan.ai_profiles.map(p => ({...p, persona:{...p.persona, play_style:defaultPlayStyles[seats.indexOf(p.seat)]}}))};
}
export function diversifyStrategies(plan: TablePlan): TablePlan {
  const styles = ['evidence','contradictions','relationships','pressure','balanced'] as const;
  return {...plan,ai_profiles:plan.ai_profiles.map((p,i) => ({...p,persona:{...p.persona,strategy:styles[i % styles.length]}}))};
}
export function admissionStatus(seat?: AdmissionSeat) {
  if (!seat) return '待预检';
  const names: Record<string,string> = {ready:'配置可用', qualified:'历史资格', not_tested:'待检查配置', failed:'历史未通过', running:'历史考试',
    incomplete:'考试未完成', cancelled:'考试已取消', simulated:'仅模拟通过', unavailable:'连接不可用', unsupported:'暂不支持', not_qualified:'资格无效'};
  return names[seat.status] ?? '资格无效';
}

export function realStartBody(plan: TablePlan, seed: string, admission: Admission, operation: string, confirmed: boolean) {
  const body = planBody(plan, seed);
  if (!confirmed) throw new Error('请明确确认真实对局费用。');
  if (!admissionReady(admission) || admission.seats.length !== (body.roster?.players ?? 6) - 1 ||
    (admission.roster && JSON.stringify({...cleanRoster(admission.roster),last_words:admission.roster.last_words ?? true}) !== JSON.stringify({...cleanRoster(body.roster ?? sixRoster()),last_words:body.roster?.last_words ?? true})) ||
    body.ai_profiles.some(p => !admission.seats.some(s => s.seat === p.seat && s.connection_id === p.connection_id && seatReady(s) && s.configuration_version))) {
    throw new Error('请为当前人数、阵容和所有席位重新检查配置。');
  }
  return {plan:body, operation_id:operation, confirm_cost:true,
    expected_versions:Object.fromEntries(admission.seats.map(s => [String(s.seat),s.configuration_version]))};
}
