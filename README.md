# 车序 Chexu

用 LangGraph 写的一个二手车门店助手，后端是 FastAPI + PostgreSQL，前端是 React。可以通过对话查库存、比较车辆、读检测报告、生成报价，也可以用 Tavily 搜索外部资料。

这是一个边学习边开发的项目。除了业务功能，主要在做 Agent 的执行控制：怎么限制工具权限，怎么处理审批和执行失败，以及多个 Agent 之间应该传递哪些上下文。目前用本地资料和评估任务验证这些流程。

当前版本：`0.14.0`。

## 已有功能

- 按预算和里程查询库存，比较候选车辆的当前售价和里程。
- 上传车辆报告，通过本地 MCP 解析 PDF、DOCX、TXT、Markdown，回答中附带原文片段。
- 生成报价预览。需要提交审批时暂停任务，由经理审核后继续执行。
- 将复杂需求拆成多个步骤，交给子 Agent 执行；支持补充条件、修改计划和重排剩余步骤。
- 保存用户确认的预算、用途和偏好，支持修改、删除。这里的用户是当前登录员工，还没有单独的客户档案。
- 长对话接近输入容量时，整理较早的历史内容；原始消息继续保存在数据库里。
- 通过 Tavily 搜索公开网页，展示摘要、链接和日期。
- 员工登录、会话管理、后台任务、执行进度查看、取消和失败恢复。

## Agent 是怎么运行的

请求先经过身份和会话归属检查。工作台提交的任务会存进 PostgreSQL 队列，由后台 Worker 执行，前端通过 SSE 接收进度。也保留了同步的 `/api/chat` 接口，方便调试。

普通任务的图比较简单：

```mermaid
flowchart LR
    Input[用户消息] --> Skill[选择技能]
    Skill --> Context[整理模型上下文]
    Context --> Model[调用模型]
    Model -->|工具调用| Tools[权限与预算检查]
    Tools --> Execute[执行工具]
    Execute --> Context
    Model -->|最终回答| Finish[整理结果并保存]
```

### 技能和工具

技能放在 [`skills/`](skills/) 下，每个目录里有两个文件：

```text
skill.toml    技能名称、版本、工具清单和调用上限
SKILL.md      给模型的执行说明
```

启动时先注册元数据，选中技能后才加载正文。当前有库存匹配、车辆对比、报告解读、报价协作、任务协作、个人记忆和联网检索 7 个技能。

模型每次只能看到当前技能允许使用的工具。调用到达后端后，还会经过 [`middleware.py`](src/agent/middleware.py) 检查，不能靠模型自己遵守权限。运行时会限制调用次数和执行时间，也会检查取消请求。

技能正文和版本会随运行保存快照。恢复旧任务时使用原来的说明，同时检查工具是否仍然允许调用。

### 规划和子 Agent

在工作台选择“任务协作”，或传入 `skill_id: "deal_review"`，模型会先提交结构化计划，再由主 Agent 委派子图执行。

每个子 Agent 有自己的消息历史和检查点命名空间。交接内容只包括当前目标、输入条件和依赖步骤的结果，不直接复制整段父会话。计划最多 6 步，无依赖的只读步骤最多 2 个并行；报价安排在最后。

用户可以补充资料、改条件或重排剩余任务。调整请求带上计划版本和任务轮次，避免旧页面的操作覆盖新计划。相关代码在 [`planning.py`](src/agent/planning.py)、[`subagents.py`](src/agent/subagents.py) 和 [`plan_control.py`](src/agent/plan_control.py)。

### 审批和恢复

报价先生成预览，用户明确要求提交时才申请审批。经理审核期间，LangGraph 保存现场并暂停；审核后从检查点继续。

模型调用或工具执行失败时，也会保留运行记录，由用户手动恢复或结束本轮。恢复不会重新获得一份调用额度，之前已经用掉的次数仍然计入限制。取消任务不会撤销此前保存的报价。

执行入口在 [`runtime.py`](src/agent/runtime.py)，检查点使用 PostgreSQL 的 `PostgresSaver`。

### 记忆和长对话

长期记忆按员工保存，目前只有预算、用途和偏好三类。模型可以提出待确认记录，确认操作由用户完成。未确认内容不会注入后续对话。

长对话处理是另一套逻辑：模型调用前检查输入容量，按整轮整理较早的历史。现在用的是规则摘录，没有额外调用模型做摘要，也没有向量库。历史提到过的条件不会自动变成长期记忆，旧价格也不能当作当前报价。

对应代码是 [`memories.py`](src/services/memories.py) 和 [`conversation_context.py`](src/agent/conversation_context.py)。

### 报告和搜索

报告解析走本地 `chexu-report-parser` MCP 服务，通过 stdio 通信，由后端按需启动。模型传报告编号，服务端负责找到对应文件。引用会检查是否来自实际读取的片段，但结论是否得到原文充分支持仍需要复核。

Tavily 直接调用搜索 API，没有再套一层 MCP。结果作为网页参考返回，不会写成门店库存，也不能据此判断某辆车的事故情况。当前还没有专业估值或真实成交行情接口。

## 目录

```text
Chexu/
├─ src/
│  ├─ agent/          # 图、运行时、技能、规划、上下文、工具
│  ├─ api_view/       # FastAPI 接口
│  ├─ services/       # 库存、任务、审批、记忆、搜索等服务
│  ├─ domain/         # 数据结构和业务约束
│  ├─ database/       # ORM、连接、检查点、员工管理
│  ├─ mcp/            # 报告解析的 MCP 客户端和服务端
│  ├─ reports/        # 文件校验、存储和解析
│  ├─ evaluation/     # 任务评估
│  └─ security.py     # 密码和令牌处理
├─ frontend/          # React + TypeScript + Vite
├─ skills/            # 技能说明和元数据
├─ migrations/        # Alembic 迁移
├─ tests/             # 测试与数据库集成检查
├─ evals/             # 评估任务和样本
├─ fixtures/          # 固定样例资料
├─ scripts/           # 本地开发与评估脚本
├─ docs/development/  # 开发记录
├─ .env.example
├─ pyproject.toml
└─ uv.lock
```

业务数据、任务队列和检查点都放在 PostgreSQL，上传报告放在本地 `data/reports`。目前不需要另外部署 Redis 或向量数据库。

## 本地运行

需要 Python 3.11～3.13、uv、Node.js 22.12+、npm 和已启动的 PostgreSQL。当前本地开发用的是 PostgreSQL 18.6。

模型服务需要兼容 OpenAI 接口，并支持工具调用。Tavily 密钥可选，不配置时仍可使用其他功能。

下面以 Windows PowerShell 为例，命令从项目根目录执行。

### 1. 依赖和配置

```powershell
$env:UV_CACHE_DIR = Join-Path (Get-Location) ".cache/uv"
uv sync --frozen

if (-not (Test-Path .env)) {
    Copy-Item .env.example .env
}
```

在 `.env` 中填写自己的连接信息：

```dotenv
CHEXU_MODEL_BASE_URL=https://your-provider.example/v1
CHEXU_MODEL_NAME=your-tool-calling-model
CHEXU_MODEL_API_KEY=replace-with-your-key
CHEXU_DATABASE_URL=postgresql+psycopg://chexu:replace-with-your-database-password@127.0.0.1:5432/chexu

# 根据网络情况选择是否使用环境代理
CHEXU_MODEL_USE_ENV_PROXY=true
CHEXU_TAVILY_USE_ENV_PROXY=true

# 联网检索需要，其他功能可留空
CHEXU_TAVILY_API_KEY=
```

其他选项见 [`.env.example`](.env.example)。配置从项目根目录读取，修改后重启后端生效。数据库密码含特殊字符时，需要在连接字符串中做 URL 编码。

### 2. 数据库

先创建 `chexu` 数据库和应用账号。已有可用数据库时，直接修改连接配置即可。

例如在管理员的 `psql` 中执行：

```text
CREATE ROLE chexu LOGIN;
\password chexu
CREATE DATABASE chexu OWNER chexu;
```

`\password` 会提示输入密码。然后回到项目终端初始化：

```powershell
uv run python -m src.database.check
uv run alembic upgrade head
uv run python -m src.database.checkpoints
```

前一组迁移负责业务表，最后一条负责 LangGraph 检查点表。两者都要初始化，应用账号需要在该数据库中创建 schema 和表的权限。

仓库里的 `scripts/postgres.ps1` 是原开发环境的启停脚本，默认目录在 E 盘。自行安装的 PostgreSQL 可以直接用自己的服务管理方式，不必照搬这个路径。

### 3. 员工账号

```powershell
uv run python -m src.database.employees --username manager01 --role manager
uv run python -m src.database.employees --username sales01 --role sales
```

在终端按提示设置密码，长度为 12～128 位。没有默认登录密码，也没有开放注册页面。销售账号用来发起报价，经理账号用来审核。

### 4. 前端和后端

先构建前端：

```powershell
$env:npm_config_cache = Join-Path (Get-Location) ".cache/npm"
Set-Location frontend
npm ci
npm run build
Set-Location ..
```

再启动后端：

```powershell
uv run uvicorn src.api_view.app:app --host 127.0.0.1 --port 18080
```

- 工作台：<http://127.0.0.1:18080/>
- 接口文档：<http://127.0.0.1:18080/docs>

构建后的页面由 FastAPI 提供，后端默认会带一个任务 Worker。前端开发时可以在 `frontend` 目录另开终端运行 `npm run dev`，访问 5173；它会把 API 请求转发到 18080。

首次运行没有库存，需要先在“车辆资料”里录入车辆，再上传对应报告。也可以从接口文档登录授权后调用 `POST /api/vehicles`。

## 测试和评估

隔离测试和技能路由检查：

```powershell
uv run python -m unittest discover -s tests -p "test_*.py" -q
uv run python scripts/evaluate_skills.py
```

数据库集成检查需要 PostgreSQL，建议使用单独的测试数据库配置：

```powershell
uv run python -m unittest discover -s tests/integration -p "test_*.py" -q
```

真实模型评估单独运行，不需要先启动后端：

```powershell
uv run python scripts/evaluate_tasks.py --list
uv run python scripts/evaluate_tasks.py --limit 3 --max-model-calls 10
```

当前有 9 个任务场景，覆盖库存、对比、报价、审批、报告、记忆、长对话和规划。评估会创建临时业务及检查点 schema，结束后清理，结果保存在 `data/evals/`。它使用 `.env` 中的模型，会产生调用费用。Tavily 搜索还没有纳入这组真实任务评估。

测试里的模型和工具替身用来检查程序流程，真实评估才会调用模型。两类结果要分开看；报告引用和回答内容也仍需要人工复核。

## 还没做的部分

当前按单门店开发，尚未做多租户隔离。报告没有 OCR，扫描件需要先转成可提取文本的文件。外部行情目前只有网页搜索，没有接专业估值、VIN 车史或真实成交数据。

也没有开放任意代码执行或终端操作，本地 MCP 只是报告解析进程。部署目前以本地运行为主，压测、集中监控和备份恢复还需要继续补。

## 几个运行时容易遇到的问题

- 模型报连接错误时，先检查地址和密钥。如果环境代理不可用、直连正常，可以设置 `CHEXU_MODEL_USE_ENV_PROXY=false`。Tavily 有独立的代理开关，修改后都需要重启后端。
- 页面看不到工作台时，检查是否生成了 `frontend/dist/index.html`，以及是否在构建后启动了后端。
- 18080 被占用时，先确认是不是已经开着一个后端进程。修改后端端口时，也要改 `frontend/vite.config.ts` 中的代理目标。

`.env`、上传资料、日志、缓存、依赖目录和构建结果已放进 [`.gitignore`](.gitignore)。公开仓库保留 `.env.example` 和依赖锁文件即可；样例文件和截图上传前需要去掉个人或客户信息。
