# 本机 PostgreSQL 环境

本项目开发环境使用 PostgreSQL 18.6 的 Windows x64 二进制包。
程序、数据库数据、下载文件和日志存放于 E 盘。

| 内容 | 路径 |
| --- | --- |
| 数据库程序 | `E:\PostgreSQL\18.6\pgsql` |
| 数据目录 | `E:\PostgreSQL\data\chexu` |
| 日志 | `E:\PostgreSQL\logs` |
| 下载文件 | `E:\PostgreSQL\downloads` |
| 安装临时目录 | `E:\PostgreSQL\tmp` |
| 本机认证文件 | `E:\PostgreSQL\secrets` |

## 连接信息

- 地址：`127.0.0.1`
- 端口：`5432`
- 数据库：`chexu`
- 应用账号：`chexu`
- 认证方式：`scram-sha-256`

应用账号拥有 `chexu` 数据库，但不具有超级用户、创建角色或创建其他数据库的权限。
密码由本机安装流程生成；连接地址写入项目 `.env` 的 `CHEXU_DATABASE_URL`，不要提交该文件。
管理员与应用账号的 psql 认证文件只保存在本机 secrets 目录。

## 启停与连接

在项目根目录运行：

```powershell
powershell -NoProfile -ExecutionPolicy RemoteSigned -File scripts\postgres.ps1 status
powershell -NoProfile -ExecutionPolicy RemoteSigned -File scripts\postgres.ps1 start
powershell -NoProfile -ExecutionPolicy RemoteSigned -File scripts\postgres.ps1 shell
powershell -NoProfile -ExecutionPolicy RemoteSigned -File scripts\postgres.ps1 stop
```

使用 Windows 自带的 PowerShell；上述命令也可以在 CMD 中执行。
`-ExecutionPolicy RemoteSigned` 仅对这次启动的进程生效，允许运行本地编写的脚本，不修改系统或用户的全局执行策略。
进入 psql 后用 `\q` 退出。

数据库作为当前用户的后台进程运行，没有注册开机自启服务。
重启电脑后，需要先执行 start，再运行依赖数据库的后端功能。
stop 使用 PostgreSQL 的正常快速关闭方式，不删除数据。

## 与项目代码的关系

安装数据库是准备一个保存数据的独立服务；Python 通过数据库驱动连接它。

当前已经加入 SQLAlchemy 和 `psycopg[binary]`：

- `src/agent/config.py` 的 `database_url` 从 `CHEXU_DATABASE_URL` 读取连接地址。
- `src/database/connection.py` 创建 Engine 和 SessionFactory，统一管理数据库访问。
- `src/database/check.py` 提供实际的数据库连接检查。

在项目根目录执行：

```powershell
uv run python -m src.database.check
```

成功输出：

```text
Database connected: chexu (user: chexu)
```

命令执行的 `SELECT current_database(), current_user` 只读取连接信息，不创建业务表。
失败时命令返回非零退出码，并提示检查数据库状态和配置，不打印连接密码。

### 连接管理的工作方式

1. `create_engine(...)` 创建连接管理对象，使用连接池复用底层连接；此时不会立即连接数据库。
2. `SessionFactory()` 为一次操作创建独立会话；Session 也不是底层网络连接本身。
3. 第一次执行 SQL 时，Session 从 Engine 获取连接。
4. `with` 结束时关闭 Session，释放其连接资源；连接通常归还连接池，并非每次断开网络连接。
5. 检查命令退出前调用 `engine.dispose()`，关闭池中的空闲连接。

`pool_pre_ping=True` 在连接取出时检查可用性，帮助处理数据库重启后的旧连接。
它不会替代事务失败后的重试逻辑。
`connect_timeout=5` 限制建立连接时的等待时间，不是 SQL 的执行超时。
`hide_parameters=True` 隐藏 SQLAlchemy 日志和异常中的 SQL 绑定参数，并不替代对连接地址的保密处理。

查询工具使用 `with SessionFactory() as session`，退出时关闭 Session，不自动提交写入。
录入接口使用 `with SessionFactory.begin() as session`，成功退出时提交，异常时回滚。
目前已通过首个迁移建立车辆表，车辆录入接口和 `get_vehicle` 查询工具均已连接该表；工具不再读取固定 JSON 文件。

依赖缓存和安装临时文件位于项目 `.cache` 目录，该目录已加入 `.gitignore`。

## 车辆表与迁移

`src/database/base.py` 定义 SQLAlchemy 映射基类；`src/database/models.py` 的
`VehicleRecord` 描述 `vehicles` 表。

| 字段 | 作用 | 约束 |
| --- | --- | --- |
| `id` | 数据库内部 ID | 自动生成，主键 |
| `vehicle_id` | 门店使用的车辆编号 | 必填、唯一、最长 32 个字符，不能是空字符串或全为空格 |
| `title` | 车辆名称 | 必填、最长 200 个字符，不能是空字符串或全为空格 |
| `price_cny` | 当前车辆售价，单位为整数元 | 必填、非负 |
| `mileage_km` | 里程，单位为公里 | 必填、非负 |
| `is_demo` | 是否为模拟数据 | 默认 false |
| `created_at` | 入库时间 | 数据库生成，带时区 |

目前按单门店建模，车辆编号在整张表内唯一。后续支持多门店时，需要迁移为门店和车辆编号的联合唯一约束。
价格暂时延续现有接口的整数元单位；涉及按分计价时，应统一调整存储与接口的金额表达。

`VehicleRecord` 描述数据库记录，`src/api_view/schemas.py` 中的 `Vehicle` 描述接口输出；
数据库内部 ID 和创建时间不必自动暴露到所有接口。

迁移文件保存结构变更历史，定义 Python 模型不会自动执行建表。
首个迁移为 `migrations/versions/0001_create_vehicles.py`，版本号为 `0001`。
`alembic.ini` 指定迁移文件位置，`migrations/env.py` 使用项目 Engine 连接数据库并加载模型元数据。
数据库地址仍从 `.env` 读取，没有写入迁移配置文件。

在项目根目录执行：

```powershell
uv run alembic upgrade head
uv run alembic current
uv run alembic check
```

三条命令分别用于应用迁移、查看数据库当前版本、检查模型与数据库之间是否还有待生成的变更。
首次升级会创建 `vehicles` 和 Alembic 自己使用的版本记录表 `alembic_version`。
已经应用到当前版本时，再次执行 upgrade 不会重复建表。

修改模型后，使用 `uv run alembic revision --autogenerate -m "说明本次变更"` 生成候选迁移，
审查内容后再升级。自动生成不能替代人工检查，尤其是字段重命名、数据转换及部分约束变更。
迁移里的 downgrade 描述逆向结构操作；首个版本的 downgrade 会删除车辆表，不用于日常启停。

### 数据库约束检查

先应用迁移，再执行真实 PostgreSQL 集成检查：

```powershell
uv run python -m unittest discover -s tests/integration -v
```

检查真实写入和读取、数据库默认值、编号唯一性、负价格、负里程和空名称/编号。
每项检查使用事务回滚，不保留车辆记录；身份序列的取号可能产生正常的间隔。

原有 Agent 检查仍通过 `uv run python -m unittest discover -s tests -v` 单独执行。

车辆录入接口的代码流程和验证方式见 [车辆录入说明](vehicle-entry.md)。
集成检查目录也包含录入接口检查：它们会真正提交，再仅清理本次生成编号对应的检查记录。

## 官方来源

- [PostgreSQL Windows 下载说明](https://www.postgresql.org/download/windows/)
- [EDB 二进制包下载](https://www.enterprisedb.com/download-postgresql-binaries)
- [EDB 发布的该包 SHA-256](https://github.com/EnterpriseDB/edb-installers/issues/733)
- [initdb：初始化数据目录](https://www.postgresql.org/docs/current/app-initdb.html)
- [pg_ctl：管理数据库进程](https://www.postgresql.org/docs/current/app-pg-ctl.html)
- [SQLAlchemy Engine 配置](https://docs.sqlalchemy.org/en/21/core/engines.html)
- [SQLAlchemy Session 基础](https://docs.sqlalchemy.org/en/21/orm/session_basics.html)
- [Psycopg 安装说明](https://www.psycopg.org/psycopg3/docs/basic/install.html)
- [Alembic 迁移教程](https://alembic.sqlalchemy.org/en/latest/tutorial.html)
- [Alembic 自动生成与审查](https://alembic.sqlalchemy.org/en/latest/autogenerate.html)
