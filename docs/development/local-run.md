# 本地开发与验证

这里记录环境准备、初始化命令、测试和常见运行问题。项目介绍见 [README](../../README.md)。

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

其他选项见 [`.env.example`](../../.env.example)。配置从项目根目录读取，修改后重启后端生效。数据库密码含特殊字符时，需要在连接字符串中做 URL 编码。

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

## 几个运行时容易遇到的问题

- 模型报连接错误时，先检查地址和密钥。如果环境代理不可用、直连正常，可以设置 `CHEXU_MODEL_USE_ENV_PROXY=false`。Tavily 有独立的代理开关，修改后都需要重启后端。
- 页面看不到工作台时，检查是否生成了 `frontend/dist/index.html`，以及是否在构建后启动了后端。
- 18080 被占用时，先确认是不是已经开着一个后端进程。修改后端端口时，也要改 `frontend/vite.config.ts` 中的代理目标。

`.env`、上传资料、日志、缓存、依赖目录和构建结果已放进 [`.gitignore`](../../.gitignore)。公开仓库保留 `.env.example` 和依赖锁文件即可；样例文件和截图上传前需要去掉个人或客户信息。
