# 源码阅读与评审指南

这是一份可公开的技术导览，帮助读者理解当前实现。真实对局和本机诊断记录不在仓库中。

## 先看这些文件

| 关注点 | 入口 |
| --- | --- |
| 确定性规则、胜负、技能与可见视图 | `backend/werewolf/engine.py`、`roster.py` |
| 调度、单步预取、暂停恢复、事务提交 | `backend/werewolf/service.py`、`storage.py` |
| 模型请求与 JSON 行动校验 | `backend/werewolf/prompts.py`、`agent.py` |
| 每席私密记忆与事实核对 | `backend/werewolf/player_state.py`、`player_logic.py` |
| 三份玩家 MD 及旧局兼容 | `skills/werewolf-player/`、`backend/werewolf/rulebook.py` |
| 并行拿牌准备 | `backend/werewolf/opening.py`、`service.py` |
| 供应商接入与额度 | `backend/werewolf/providers.py`、`deepseek.py`、`qwen.py`、`siliconflow.py`、`decision_budget.py` |
| 账号、密钥加密、免费配置检查 | `backend/werewolf/accounts.py`、`account_api.py`、`real_games.py`、`table_plan.py` |
| 页面入口、对话和播放顺序 | `frontend/src/App.tsx`、`useGame.ts`、`Chat.tsx`、`typing.ts` |
| 配置及恢复体验 | `frontend/src/AccountSettings.tsx`、`TablePlanner.tsx`、`RecoveryNotice.tsx` |
| 离线回归 | `backend/tests/`、`frontend/tests/` |

## 当前设计

- 引擎负责规则执行与结算，模型只能从本席 `player_view()` 和合法行动 Schema 选择。服务器完整快照不能进入模型或普通玩家视图。
- 当前新局提示版本为 `player-agent-v11-wolf-coordination`。每次提供游戏、本席身份、实际阵容规则三份 MD，不依赖模型自主读本机文件。
- 模型一次返回 `action` 和可为空的 `player_update`；状态和行动原子提交。真实身份、查验、技能权限不由表演身份或私密意图改写。
- 每个 AI 拿牌后生成最多 40 字的私密意图，最多 3 席并行，失败跳过；正式阶段保留顺序，狼人当轮回应逐条队内可见。
- 新局屠边：狼人全灭好人胜；村民全灭或神职全灭狼人胜。七身份为狼人、村民、预言家、女巫、猎人、守卫、骑士。最终以实际配置和合法行动为准。
- 历史局冻结提示、规则、供应商参数和配置指纹。旧 Skill、资格模块仍保留用于兼容和回归，不代表网页重新要求资格考试。
- 提示希望形成有性格、立场和关系的玩家，同时仍以自己阵营获胜为行动目标；允许误判、诈身份和冒险，不靠强制分票制造差异。

## 值得评审的方向

1. 信息不足时能否少说，是否区分真实事实、口头声明、未知死因和私密表演。
2. 同模型多个玩家是否持续体现各自立场，是否回应确实已经听到的发言。
3. 狼队能否理解方案的前提并隐蔽配合，技能选择能否遵循基本职责。
4. 等待时间、思考额度、非法输出重试和文字播放的组合是否合理。当前错误记录未细分所有行动校验原因，不能从 `invalid_action` 单独断言格式错误。
5. 预取、并行准备、取消迟到结果、重启恢复是否严格保护席位隔离与幂等。

请以源码与离线测试为证据。测试通过不代表模型会玩，也不代表某供应商参数或模型效果已全部实测。仓库保留本地运行定位，尚未完成公网多用户部署验收。
