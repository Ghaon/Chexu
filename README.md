# 车序 Chexu

车序是一个面向二手车门店的 Agent 应用，主要用于销售选车和报价协作。销售可以通过对话筛选库存、比较车辆、查阅检测资料和准备报价，经理在审批中心处理报价申请。

项目以门店库存和上传报告作为车辆信息来源，同时支持 Tavily 联网检索。一次需求可以单独查询，也可以拆成多个步骤执行；任务进度、资料引用和审批结果都在工作台中查看。

后端使用 **FastAPI、LangGraph 和 PostgreSQL**，前端使用 **React、TypeScript 和 Vite**。

## 使用场景

**按条件选车。** 输入预算和里程要求，从门店库存中筛选候选，再比较当前售价、里程等信息。后续可以继续引用会话中选中的车辆，不必重复输入全部条件。

**核对车辆资料。** 上传检测或维保报告，读取相关片段，整理报告记录、未知信息和需要复检的事项。回答附带原文引用，方便销售回到资料中核对。

**准备和审核报价。** 为选定车辆生成报价预览，列明优惠和待确认项目。销售决定提交后，任务进入经理审批；审核通过并完成后续执行，才保存确认报价。

**查询外部信息。** 搜索车型资料、行业信息和公开政策，查看网页摘要、来源与日期。网页信息作为参考，与门店库存和车辆报告分别展示。

例如，在工作台选择“任务协作”后，可以提出：

> 从库存里找十万元以内、里程不超过六万公里的车，比较候选车辆。如果有检测报告，列出需要注意的记录，再为我选中的车辆生成报价预览，先不要提交审批。

任务缺少车辆选择或必要资料时，可以补充信息后继续，也可以调整预算、候选车和剩余步骤。

## 业务流程

下面展示一条从选车到报价的典型路径。库存查询、报告解读和联网检索也可以单独使用。

```mermaid
flowchart TB
    subgraph Selection["选车与资料核对"]
        Request["销售输入条件<br/>预算、里程、候选车辆"] --> Inventory["筛选门店库存"]
        Inventory --> Compare["比较候选车辆"]
        Compare -->|直接查看| Result["查看候选结果与资料依据"]
        Compare -->|需要核对报告| Report["读取检测资料<br/>列出记录与待核实事项"]
        Report --> Result
    end

    subgraph Quotation["报价与人工审批"]
        Preview["生成报价预览"] -->|销售提交审批| Review{"经理审核"}
        Review -->|通过后继续执行| Saved["保存确认报价"]
        Review -->|拒绝| Rejected["返回审核结果"]
    end

    Result -->|需要报价| Preview
```

报价预览可以直接查看，审批由销售另行发起。选车结果和报价结果都保留在对应会话中，已提交的审批可以在审批中心查看。

## 工作台功能

- **会话与任务**：多轮对话、历史会话、后台任务进度、取消和失败恢复。
- **车辆资料**：录入车辆，关联上传 PDF、DOCX、TXT 或 Markdown 报告，查看报告片段。
- **任务协作**：展示执行步骤，支持补充条件、修改目标、重排剩余任务。
- **审批中心**：销售查看或取消自己的申请，经理审核报价。
- **偏好记忆**：当前登录员工确认自己的预算、用途和偏好，支持查看、修改和删除。
- **资料来源**：查看报告引用及联网检索返回的网页链接。

销售与经理使用不同角色登录。库存按门店共享，会话和个人记忆按员工隔离。界面支持桌面三栏布局和手机访问。

## 系统架构

工作台提交后台任务，Worker 从 PostgreSQL 队列领取任务，再交给 Agent 执行。模型只能使用当前技能开放的工具，工具调用仍需经过后端检查。

```mermaid
flowchart TB
    UI["React 工作台"] -->|任务提交与结果查询| API["FastAPI<br/>身份与会话检查"]
    API -.->|SSE 执行进度| UI
    API --> Queue["PostgreSQL 持久化任务队列"]
    Queue --> Runtime["Worker / Agent Runtime"]

    subgraph Agent["LangGraph 执行层"]
        Lead["主 Agent<br/>技能与模型调用"]
        Child["规划与子 Agent<br/>依赖步骤、独立模型上下文"]
        Gate["工具执行中间件<br/>权限、调用次数、超时、取消"]
        Lead -->|单项任务| Gate
        Lead -->|组合任务| Child
        Child --> Gate
    end

    Runtime --> Lead
    Gate --> Business["库存 / 报价 / 记忆工具"]
    Gate --> Parser["本地 MCP 报告解析"]
    Gate --> Search["Tavily 搜索 API"]

    Business --> Database["PostgreSQL<br/>业务数据、运行记录、检查点"]
    Parser --> Files["本地报告文件"]
```

主 Agent 和子 Agent 使用配置的模型服务，分别准备上下文。任务队列、业务表、运行记录和检查点共用 PostgreSQL，执行状态由 Runtime 保存和恢复。报告文件保存在本地目录；MCP 解析进程由后端按需启动。前端通过 SSE 接收执行阶段事件，任务结束后读取结构化结果。

### Agent 执行设计

**技能。** [`skills/`](skills/) 中有库存匹配、车辆对比、报告解读、报价协作、任务协作、个人记忆和联网检索 7 个技能。`skill.toml` 定义工具与调用上限，`SKILL.md` 提供执行说明。启动时加载元数据，选中技能后加载正文，并为运行保存版本快照。

**规划与子 Agent。** 多步骤任务使用 `deal_review` 技能。主 Agent 提交结构化计划，子 Agent 使用各自的消息历史和检查点命名空间，只接收当前目标与声明依赖的结果。计划最多 6 步，独立的只读步骤最多 2 个并行，报价安排在最后。

**运行控制。** 运行配置绑定员工身份、技能、调用预算和截止时间。工具中间件统一检查权限、额度、超时及取消请求。计划调整带有轮次和版本检查，避免重复或陈旧请求覆盖当前任务。

**审批与恢复。** 使用 LangGraph 检查点保存执行状态，支持报价审批暂停、审核后续接，以及失败后的显式恢复。恢复沿用累计调用预算；取消不撤销此前保存的确认报价。

**记忆与上下文。** 模型提出的记忆需要用户确认后才会用于后续任务。长对话在模型调用前按容量整理较早的完整轮次，原始消息仍保存在数据库中；历史价格不会替代当前查询。

**工具与资料。** 库存、报价和记忆通过业务工具访问。报告通过本地 MCP 解析并校验引用片段，公开资料通过 Tavily Search API 检索。网页与文档内容不具有修改工具权限或审批规则的能力。

**运行记录与评估。** 保存任务状态、模型及工具事件、调用次数和可用的 token 用量。仓库提供技能路由检查、隔离测试、数据库集成检查和真实模型任务评估。

主要实现集中在 [`src/agent/`](src/agent/)；业务接口和服务分别位于 [`src/api_view/`](src/api_view/) 与 [`src/services/`](src/services/)。

## 本地运行

需要 Python **3.11～3.13**、uv、Node.js **22.12+**、npm 和 PostgreSQL。模型服务需要兼容 OpenAI 接口并支持工具调用；Tavily 密钥仅联网检索需要。

以下命令以 Windows PowerShell 为例，从项目根目录执行。数据库应已启动，并创建好应用数据库与账号。完整准备步骤见 [本地开发与验证](docs/development/local-run.md)。

### 配置后端

```powershell
uv sync --frozen

if (-not (Test-Path .env)) {
    Copy-Item .env.example .env
}
```

在 `.env` 填写自己的配置：

```dotenv
CHEXU_MODEL_BASE_URL=https://your-provider.example/v1
CHEXU_MODEL_NAME=your-tool-calling-model
CHEXU_MODEL_API_KEY=replace-with-your-key
CHEXU_DATABASE_URL=postgresql+psycopg://chexu:replace-with-your-database-password@127.0.0.1:5432/chexu
CHEXU_TAVILY_API_KEY=
```

其余选项及代理开关见 [`.env.example`](.env.example)，配置变更在后端重启后生效。

### 初始化数据库和员工账号

```powershell
uv run python -m src.database.check
uv run alembic upgrade head
uv run python -m src.database.checkpoints

uv run python -m src.database.employees --username manager01 --role manager
uv run python -m src.database.employees --username sales01 --role sales
```

员工密码由终端交互设置，没有默认密码。业务表与 LangGraph 检查点表需要分别初始化。

### 构建页面并启动

```powershell
Set-Location frontend
npm ci
npm run build
Set-Location ..

uv run uvicorn src.api_view.app:app --host 127.0.0.1 --port 18080
```

- 工作台：<http://127.0.0.1:18080/>
- API 文档：<http://127.0.0.1:18080/docs>

构建后的页面由 FastAPI 提供。前端开发时可在 `frontend` 目录另开终端运行 `npm run dev`，访问 5173。

首次运行需先在“车辆资料”页录入车辆，再上传对应报告。多步骤任务在工作台选择“任务协作”；只查询库存或解读报告时，可直接选择相应功能。

## 项目结构

```text
Chexu/
├─ src/
│  ├─ agent/          # LangGraph、运行时、技能、规划、上下文与工具
│  ├─ api_view/       # FastAPI 路由与认证
│  ├─ services/       # 任务、库存、审批、记忆、搜索等服务
│  ├─ domain/         # 数据结构和业务约束
│  ├─ database/       # ORM、迁移连接、检查点与员工管理
│  ├─ mcp/            # 报告解析客户端和服务端
│  ├─ reports/        # 文件校验、存储与解析
│  ├─ evaluation/     # 真实任务评估
│  └─ security.py     # 密码与令牌处理
├─ frontend/          # React 工作台
├─ skills/            # 技能元数据与执行说明
├─ migrations/        # Alembic 数据库迁移
├─ tests/             # 隔离测试与集成检查
├─ evals/             # 评估任务和样本
├─ fixtures/          # 固定样例资料
├─ scripts/           # 启动辅助与评估脚本
├─ docs/              # 运行说明与开发记录
├─ .env.example
├─ pyproject.toml
└─ uv.lock
```

## 测试与评估

```powershell
uv run python -m unittest discover -s tests -p "test_*.py" -q
uv run python scripts/evaluate_skills.py
uv run python scripts/evaluate_tasks.py --list
```

真实模型评估集包含 9 个场景，覆盖库存筛选、车辆对比、报价、审批恢复、报告引用、记忆、长对话和多 Agent 规划。运行实际评估会调用配置的模型，结果保存在 `data/evals/`；联网检索尚未纳入这组场景。

评估检查数据库事实、操作结果和原文引用，语义质量仍需人工复核。数据库集成检查及评估命令见 [本地开发与验证](docs/development/local-run.md#测试和评估)。

## 能力范围

当前按单门店场景实现。个人记忆属于登录员工，尚未扩展成客户档案；报告解析支持可提取文本的文件，暂不包含 OCR。

外部数据来自公开网页搜索，尚未接入专业估值、VIN 车史或真实成交行情。报告片段与网页来源可供核对，不能代替车辆实检或专业估值。

项目未开放任意代码执行，MCP 仅承担本地报告解析。多租户、集中监控、备份恢复及部署压测仍需按实际使用环境补充。
