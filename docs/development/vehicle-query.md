# Agent 按编号查询库存

本步目标：让 Agent 查询通过 `POST /api/vehicles` 保存的车辆，接通真实库存数据。

## 修改范围

| 文件 | 作用 |
| --- | --- |
| [vehicle_tools.py](E:/MyProjects/Chexu/src/agent/tools/vehicle_tools.py) | 将固定 JSON 读取替换为 PostgreSQL 查询 |
| [vehicle_agent.py](E:/MyProjects/Chexu/src/agent/vehicle_agent.py) | 将可预期的工具错误转成明确提示，隐藏其他异常详情 |
| [test_agent_flow.py](E:/MyProjects/Chexu/tests/test_agent_flow.py) | 使用数据库替身检查图流程、输入与错误处理 |
| [test_vehicle_api.py](E:/MyProjects/Chexu/tests/integration/test_vehicle_api.py) | 真实数据库验证录入后查询、查无记录与参数绑定 |

没有改变表结构、路由、模型配置或图的节点连接关系，无需新增迁移、依赖或环境变量。
历史文件 `fixtures/smoke_vehicle.json` 不再参与应用运行或自动检查。

## 从问题到回答

```text
员工先通过 POST /api/vehicles 录入车辆并提交事务
    ↓
员工向 POST /api/chat 提交包含车辆编号的问题
    ↓
模型申请调用 get_vehicle(vehicle_id=编号)
    ↓
ToolNode 执行工具 → 独立 Session → 查询 vehicles 表
    ↓
车辆对象 → 普通字典 → ToolMessage
    ↓
模型依据工具结果回答 → finish 返回回答和车辆卡片
```

模型提供查询参数，查询语句由后端固定代码构造。模型没有任意执行 SQL 的入口。

## 按执行顺序读代码

1. `vehicle_id.strip()` 去除编号首尾空白，与录入时一致；空编号、超过 32 字符或含空字符直接拒绝。编号仍区分大小写。
2. `SessionFactory()` 为本次查询创建 Session；工厂和 Engine 可以复用，Session 不跨工具调用共享。
3. `select(VehicleRecord).where(VehicleRecord.vehicle_id == vehicle_id)` 构造查询。左侧是车辆表的编号列，右侧是工具参数。
4. `session.scalar(...)` 执行 SQL。查到时返回 `VehicleRecord` 对象；没有结果时返回 `None`。唯一约束保证该编号最多对应一条记录。
5. 查不到时抛出受控的 `VehicleToolError`，不会编造车辆资料。
6. 查到时在 Session 关闭前读取五个字段并组成字典。即使在 `with` 内写 `return`，退出上下文的清理也会执行。
7. 查询发生 SQLAlchemy 异常时，日志只记录异常类型，对工具调用方返回受控提示。

工具没有导入 API 层的响应模型，保持“Agent 工具 → 数据库”的依赖方向。
数据库内部 ID、入库时间和不存在的事故、泡水资料不加入工具结果。

## 为什么查询不用 begin、flush、commit？

| 用法 | 本项目用途 | 正常退出时 |
| --- | --- | --- |
| `with SessionFactory() as session` | 查询车辆 | 关闭 Session，结束尚未提交的事务，归还连接 |
| `with SessionFactory.begin() as session` | 录入车辆 | 提交事务并关闭 Session；异常时回滚 |

查询不产生需要保存的修改，因此不用提交。读取也有事务，并不表示数据库中“完全没有事务”。
`select(...)` 只是语句对象；调用 `session.scalar(...)` 时才真正执行。
编号通过绑定参数传给驱动，而不是用字符串拼接进 SQL。

## 错误如何回到模型？

`handle_tool_error(error: Exception)` 是交给 ToolNode 的错误处理函数。
我们主动抛出的 `VehicleToolError` 只包含适合对外展示的内容，可以返回其文本。
其他异常返回通用失败提示，避免把连接信息或内部错误交给模型。
ToolNode 将失败标记为 `ToolMessage(status="error")`，模型可据此说明失败。

`finish` 继续检查最近一次车辆查询；失败时 `vehicle` 返回 `null`，不展示更早查询的车辆。
聊天接口返回 200 只代表聊天流程正常结束，不代表车辆一定存在。

## 验证结果

2026-10-26：12 项离线 Agent 检查、15 项真实数据库与接口集成检查通过。
真实数据库验证中，先通过录入接口保存随机编号车辆，再运行聊天接口、图和查询工具，确认工具消息与车辆卡片取到新存入的价格和里程。
检查前后车辆记录数均为 0；只清理本轮生成编号对应的记录。

模型使用测试替身，因此没有消耗真实模型请求，也不代表已经完成真实模型服务的连通性或回答质量验证。
数据库检查需要本机 PostgreSQL 已启动、迁移已执行。

```powershell
uv run python -m unittest discover -s tests -v
uv run python -m unittest discover -s tests/integration -v
```

## 参考

- [SQLAlchemy ORM SELECT](https://docs.sqlalchemy.org/en/21/orm/queryguide/select.html)
- [SQLAlchemy Session 基础](https://docs.sqlalchemy.org/en/21/orm/session_basics.html)
- [LangChain 工具](https://docs.langchain.com/oss/python/langchain/tools)
