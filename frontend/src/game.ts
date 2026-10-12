import type { Action, ActionSpec, GameEvent, Persona, PlayStyle, Role, Potion, RuleConfig } from './types.ts';

export const roles: Record<Role, string> = { wolf: '狼人', seer: '预言家', witch: '女巫', villager: '村民', hunter:'猎人', guard:'守卫', knight:'骑士' };
export const potionNames: Record<Potion, string> = {skip:'不用药', save:'使用解药', poison:'使用毒药'};
export function wolfVictoryText(rules?: RuleConfig): string {
  return !rules || rules.win_condition === 'eliminate_side'
    ? '屠边：村民全部出局或神职全部出局，狼人获胜；狼人数达到或超过好人数不直接判胜。'
    : '旧局人数规则：存活狼人不少于存活好人，狼人获胜。';
}
export function wolfObjective(rules?: RuleConfig): string {
  return !rules || rules.win_condition === 'eliminate_side'
    ? '隐藏身份，与队友协作，淘汰全部村民或全部神职。'
    : '隐藏身份，协商夜间目标，让狼人数不少于好人数。';
}
export function rosterText(rules?: RuleConfig): string {
  const r = rules ?? {version:'six-player-v3-witch',wolves:2,seers:1,witches:1,villagers:2};
  return [[r.wolves,'狼人'],[r.seers,'预言家'],[r.witches ?? 0,'女巫'],[r.hunters ?? 0,'猎人'],[r.guards ?? 0,'守卫'],[r.knights ?? 0,'骑士'],[r.villagers,'村民']]
    .filter(([n]) => Number(n) > 0).map(([n, name]) => `${n} ${name}`).join(' / ') + (r.version === 'six-player-v2' ? '（旧版规则）' : '');
}
export const phases: Record<string, string> = { night: '夜晚', day_speech: '白天发言', vote: '放逐投票',
  runoff_speech: '平票辩论', runoff_vote: '平票复投', hunter_shot:'猎人开枪', knight_duel:'白天发言', last_words:'公开遗言', finished: '对局结束' };
export const actions: Record<string, string> = { speak: '公开发言', wolf_message: '狼队回应',
  propose_kill: '提出刀人目标', kill: '提交刀人目标', inspect: '查验身份', vote: '提交投票', witch:'提交用药决定', protect:'提交守护决定', shoot:'提交开枪决定', duel:'提交决斗决定', last_words:'提交遗言' };
export const presets: Record<string, string> = {
  冷静分析: '语气冷静，结合可见证据表达判断。', 简短直接: '用简洁直白的措辞表达重点。',
  话痨: '表达活跃，愿意讲清自己的观点，但遵守本次字数限制。',
  毒舌: '措辞犀利，针对游戏内发言提出质疑。', 戏精: '语气有戏剧感，善用悬念表达自己的立场。',
};
export const defaultPlayStyles: readonly PlayStyle[] = ['serious','intuitive','observer','provocateur','playful','follower','serious','observer'];
export const playStyleNames: Record<PlayStyle, string> = {serious:'认真分析', intuitive:'凭感觉拍板', observer:'看戏观望', provocateur:'拱火试探', playful:'随性整活', follower:'顺势站队'};
export function defaultPersona(play_style: PlayStyle = 'serious'): Persona {
  return { label: '冷静分析', description: presets.冷静分析, risk: 'balanced', bluff: 'medium', strategy:'balanced', play_style };
}
export function countChars(text: string): number { return Array.from(text).length; }
export function isPrivate(event: GameEvent): boolean {
  return ['inspection', 'witch_action', 'guard_action', 'wolf_proposals', 'wolf_replies', 'wolf_decision', 'wolf_message'].includes(event.kind)
    || (event.audience != null);
}
export function visibleEvents(events: GameEvent[], privateOnly = false): GameEvent[] {
  return events.filter(e => isPrivate(e) === privateOnly);
}
const seat = (value: unknown): string => `${value} 号`;
const list = (value: unknown): string => Array.isArray(value) ? value.map(seat).join('、') : '';
export function describeEvent(event: GameEvent): { title: string; text: string } {
  const d = event.data;
  switch (event.kind) {
    case 'game_started': {
      const r = d.roster as Record<string, number> | undefined;
      return {title:`${d.players ?? 6} 席就位`, text:(r ? Object.entries(r).filter(([,n]) => n > 0)
        .map(([role,n]) => `${n} 名${roles[role as Role] ?? role}`).join('、') : '两名狼人、一名预言家、三名村民') + '。身份不公开。'};
    }
    case 'night_started': return { title: '天黑请闭眼', text: '夜间行动进行中，不公开行动顺序。' };
    case 'day_started': return {title:'白天发言开始',text:d.knight_available === false ? '轮流发言，听完大家的判断再投票。' : '轮流发言。骑士未用技能时可在自己的发言前选择决斗。'};
    case 'last_words_started': return {title:'最后的发言',text:`${list(d.seats)} 依次发表遗言；可以跳过，完成后继续结算。`};
    case 'last_words': return {title:`${seat(d.seat)}遗言`,text:String(d.text ?? '').trim() || '（放弃遗言）'};
    case 'dawn': return { title: '天亮了', text: Array.isArray(d.dead) && d.dead.length
      ? `${list(d.dead)} 昨夜出局，身份与死因不公开。` : '昨夜无人出局。' };
    case 'speak': return { title: `${seat(d.seat)}发言`, text: String(d.text ?? '') };
    case 'voting_started': return { title: d.round === 2 ? '开始复投' : '开始投票', text: '选票全部提交后统一公开。' };
    case 'vote_result': return { title: `第 ${d.round} 轮票型`, text: Object.entries(d.votes as Record<string, unknown>)
      .map(([voter, target]) => `${seat(voter)} → ${target == null ? '弃票' : seat(target)}`).join('；') };
    case 'eliminated': return { title: '放逐结果', text: `${seat(d.seat)}被放逐，身份不公开。` };
    case 'runoff_started': return { title: '出现平票', text: `${list(d.candidates)} 进入辩论，随后其他存活玩家复投。` };
    case 'no_elimination': return { title: '本轮无人出局', text: d.reason === 'tie' ? '复投仍平票，进入下一夜。' : '没有有效票，进入下一夜。' };
    case 'game_over': return { title: '对局结束', text: d.winner === 'good' ? '好人阵营获胜。' : '狼人阵营获胜。' };
    case 'inspection': return { title: '私密查验', text: `${seat(d.target)}是${d.is_wolf ? '狼人' : '好人'}。` };
    case 'witch_action': return {title:'私密用药', text: d.potion === 'skip' ? '本夜未用药。' :
      `已对 ${seat(d.target)}${d.potion === 'save' ? '使用解药' : '使用毒药'}，天亮统一结算。`};
    case 'guard_action': return {title:'私密守护', text:d.target == null ? '本夜空守。' : `本夜守护 ${seat(d.target)}，挡刀不挡毒。`};
    case 'hunter_ready': return {title:'猎人举枪', text:`${seat(d.seat)}是猎人，正在决定是否开枪。完成后判定胜负。`};
    case 'hunter_shot': return {title:'猎人开枪结果', text:d.target == null ? `${seat(d.seat)}放弃开枪。` : `${seat(d.seat)}开枪带走 ${seat(d.target)}。`};
    case 'knight_duel': return {title:'骑士决斗', text:d.success ? `${seat(d.seat)}决斗成功，${seat(d.target)}是狼人并出局。若未结束，立即入夜。` : `${seat(d.seat)}决斗失败，骑士自己出局，继续白天。`};
    case 'wolf_proposals': return { title: '狼队刀人提案', text: Object.entries(d.proposals as Record<string, {target: number; text: string}>)
      .map(([s, p]) => `${seat(s)}提议刀 ${seat(p.target)}：${p.text}`).join('\n') };
    case 'wolf_replies': return { title: '狼队协商回应', text: Object.entries(d.replies as Record<string, string>)
      .map(([s, text]) => `${seat(s)}：${text}`).join('\n') };
    case 'wolf_decision': return { title: '狼队最终决定', text: `刀人目标为 ${seat(d.target)}。${d.method === 'unanimous_proposals' ? '提案一致，直接执行。' : '由最终票型裁定。'}` };
    case 'wolf_message': return { title: `${seat(d.seat)}狼队消息`, text: String(d.text ?? '') };
    default: return { title: '局面更新', text: event.kind };
  }
}
export function makeAction(spec: ActionSpec, target: string, text: string, potion?: Potion): Action {
  const result: Action = { kind: spec.kind };
  if (spec.kind === 'witch') {
    if (!potion || !spec.potions || !(potion in spec.potions)) throw new Error('请选择本次可用的药剂或不用药。');
    if (potion === 'skip') return {kind:'witch', potion, target:null};
    const value = Number(target);
    if (!target || !spec.potions[potion]?.includes(value)) throw new Error('请先选择一个合法用药目标。');
    return {kind:'witch', potion, target:value};
  }
  if (spec.targets) {
    if (target === 'abstain' && spec.allow_abstain) result.target = null;
    else {
      const value = Number(target);
      if (!target || !spec.targets.includes(value)) throw new Error('请先选择一个合法目标。');
      result.target = value;
    }
  }
  if (spec.max_length != null) {
    if (!text.trim() && !spec.allow_empty) throw new Error('请填写发言内容。');
    if (countChars(text) > spec.max_length) throw new Error(`发言不能超过 ${spec.max_length} 字。`);
    result.text = text;
  }
  return result;
}
export function pauseReason(reason: string | null): string {
  return ({ server_restarted: '后端已重启，局面已恢复，请手动继续。', server_stopped: '后端曾停止，请手动继续。',
    mock_decision_failed: '模拟玩家调用失败，可以继续重试。', service_error: '服务异常，请查看后端日志。',
    storage_unavailable: '存档不可用，请检查磁盘及后端日志。', authentication:'供应商鉴权失败，未自动重试。',
    balance:'供应商余额不足，已停止调用。', timeout:'模型请求超时，未报告的用量保持未知。',
    transport:'网络故障，已达到重试上限。', rate_limit:'供应商限流，已达到重试上限。',
    server_error:'供应商暂时不可用，已达到重试上限。', invalid_action:'模型连续返回无效行动，已暂停。',
    request_parameters:'供应商拒绝模型或参数，请核对配置。', configuration_changed:'连接配置或密钥变化，本局不会自动换模型。可恢复原参数，或结束本局后重配。',
    connection_unavailable:'本局连接已删除或不可用，请结束本局后重新配置。',
    account_session_expired:'开局账号已退出或会话过期。请登录开局账号并恢复授权，再手动继续。',
    invalid_credential_format:'密钥格式不受支持，已停止调用。', internal_error:'调用遇到内部错误，已暂停。',
    truncated_response:'模型达到输出额度，未返回完整行动；局面保留，已暂停。', invalid_content:'模型未返回有效行动文本，已暂停。',
    invalid_envelope:'模型响应格式异常，已暂停。', user_abandoned:'本局由你结束，未判定胜负，不再调用模型。',
  } as Record<string, string>)[reason ?? ''] ?? '游戏已暂停。';
}
