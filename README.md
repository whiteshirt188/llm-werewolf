# LLM Werewolf · 夜谈

人类与多个独立 AI 玩家同桌的文字狼人杀。Python 后端维护确定性规则和私密信息，React 前端展示对话与操作，每个 AI 使用独立可见视图、性格和私密状态进行决策。

目前支持 6～9 人、七种身份、屠边结算、狼队协商、技能、投票、遗言、单步/自动推进、暂停恢复与赛后复盘。可使用 DeepSeek、百炼通义千问、硅基流动混元/GLM，也可以完全离线试玩。模型效果与游戏平衡仍在迭代。

阅读和评审从 [PUBLIC_GUIDE.md](PUBLIC_GUIDE.md) 开始；提示正文在 [skills/werewolf-player](skills/werewolf-player)，第三方设计参考见 [ATTRIBUTION.md](ATTRIBUTION.md)。

## 本地运行

需要 Python 3.14 和 Node.js 24。以下命令在仓库根目录执行：

```sh
python3.14 -m venv .venv
.venv/bin/python -m pip install -r backend/requirements.lock.txt
npm --prefix frontend ci
make play
```

打开 <http://127.0.0.1:8000/>。`make play` 构建前端并启动本地后端；服务只监听本机。需要重新构建前端时运行 `make web-build`，已有构建可直接 `make api`。可选 `source ./env.sh` 激活项目 Python 环境。

首次体验选择「离线试玩」，无需账号或 API Key。真实模型模式：本机注册/登录 → 保存模型连接 → 分配 AI 席位 → 免费检查配置 → 自行确认费用后开局。保存和配置检查不调用模型，也不验证密钥、余额或模型表现。不要把 API Key 写进源码。

千问使用百炼北京地域 Key，模型 ID 可填 `qwen3.8-flash` 或 `qwen3.6-plus`。默认关闭思考；思考开启时沿用现有基准 4096、神职 ×2、第二晚起再 ×2、最高 16384 的合计输出额度。模型、参数、规则正文和个性在开局冻结，改设置需新局生效。

每个 AI 拿牌后额外进行一次私密准备，最多 3 席并行；正式对局按规则推进，单步等待可能提前生成一个行动。准备、行动、预取及失败重试都可能产生供应商费用；未知用量不当免费。程序不硬编码 API 价格。

## 验证

```sh
make test
make lint
make web-check
make web-build
make demo
```

公开快照前，619 项后端、88 项前端检查及构建通过，测试使用本地模拟。没有为了发布仓库发起付费模型调用；真实对局表现需实玩验证。

## 公开范围

此仓库包含源码、离线测试、项目提示文档和运行说明，不包含真实账号、API Key、加密密钥、数据库、个人对局、截图、运行日志或本机交接记录。数据默认保存在忽略的 `data/`，可用 `WEREWOLF_DB_PATH` 指定路径。依赖、构建产物和运行环境也不上传。
