# 车辆录入接口

接口：`POST /api/vehicles`

该接口将门店填写的车辆资料保存到 PostgreSQL 的 `vehicles` 表。
录入流程由普通后端代码执行，不调用模型。

## 请求与响应

请求只接受四个字段：

```json
{
  "vehicle_id": "CX-0001",
  "title": "丰田凯美瑞 2.0L",
  "price_cny": 96800,
  "mileage_km": 64000
}
```

价格使用整数元，里程使用整数公里。两项数值都必须是非负整数，且不超过 PostgreSQL INTEGER 的范围。
车辆编号和名称会去除首尾空白，分别限制为 1～32 和 1～200 个字符，并拒绝数据库文本不支持的空字符。
目前车辆编号区分大小写，在单门店车辆表中唯一。

保存成功返回 `201 Created`，响应包含上述资料，以及：

- `id`：数据库生成的内部 ID。
- `created_at`：数据库生成的入库时间。
- `is_demo`：数据库默认设为 false，表示这条资料按正式库存录入。

请求中不能指定这三个服务端字段；其他未声明字段也会被拒绝。

| 情况 | HTTP 状态 |
| --- | --- |
| 保存成功 | 201 |
| 请求数据不符合要求 | 422 |
| 车辆编号已存在 | 409 |
| 数据库连接或连接池暂时不可用 | 503 |
| 其他数据库保存错误 | 500 |

## 对应代码

1. `src/api_view/schemas.py` 的 `VehicleCreate` 检查员工提交的资料。
2. `src/api_view/vehicles.py` 的 APIRouter 定义车辆接口，`app.py` 将它注册到主应用。
3. `VehicleRecord(**body.model_dump())` 把通过检查的输入转换成待保存的数据库对象。
4. `SessionFactory.begin()` 创建 Session 和事务；正常退出时提交，异常退出时回滚，并关闭 Session。
5. `session.add()` 将对象加入本次操作；`session.flush()` 发送 INSERT，取得数据库生成的字段，但此时事务还未提交。
6. `VehicleCreated.model_validate(vehicle)` 在会话关闭前读取对象属性，生成独立的响应数据。
7. 退出事务块并成功提交后，接口才返回响应。

`VehicleCreated` 的 `from_attributes=True` 允许 Pydantic 从 ORM 对象属性读取数据。
响应转换发生在事务内，避免默认的提交后属性过期和会话关闭影响序列化；对外返回发生在提交之后。

重复编号依靠数据库唯一约束处理，不使用“先查询是否存在、再插入”作为并发保护。
只将 `uq_vehicles_vehicle_id` 约束冲突转换成编号重复提示，其他数据库错误不会被误报为重复。

本次没有改变表结构，因此不需要新增迁移。
车辆 Agent 的 `get_vehicle` 已接入同一张车辆表，录入并提交成功后即可按编号查询。
查询流程与代码说明见 [车辆库存查询](E:/MyProjects/Chexu/docs/development/vehicle-query.md)。

## 验证

启动后端后可在 `/docs` 的 `vehicles` 分组查看该接口。

```powershell
uv run python -m unittest discover -s tests/integration -v
uv run python -m unittest discover -s tests -v
```

接口检查使用真实 PostgreSQL，确认请求成功后独立连接可以读到记录。
检查覆盖重复编号、同时提交相同编号、无效输入、数据库不可用以及提交失败。
还覆盖“接口录入 → Agent 工具调用 → PostgreSQL 查询 → 车辆卡片返回”。模型使用替身，数据库和图流程真实执行。
检查结束只清理本次随机生成编号对应的车辆；不删除其他车辆资料。

## 参考

- [FastAPI APIRouter](https://fastapi.tiangolo.com/tutorial/bigger-applications/)
- [SQLAlchemy Session 与事务](https://docs.sqlalchemy.org/en/21/orm/session_basics.html)
- [Pydantic 模型](https://docs.pydantic.dev/latest/concepts/models/)
