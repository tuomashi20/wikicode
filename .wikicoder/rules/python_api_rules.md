# Python 后端 API 开发架构约束

当你在编写基于 Python 的后端 API 服务（如 FastAPI, Flask, Django）时，请严格遵循以下架构级强制规范：

## 1. 模块化与解耦
- **禁止单文件巨石架构**：绝对禁止在一个单一文件（如 `main.py`）中堆砌超过 300 行的所有业务逻辑、路由、模型定义。
- **职责分离**：必须将代码合理拆分为 `routers` (路由处理)、`services` (业务逻辑)、`models` (数据库 ORM/Pydantic 模型) 和 `utils` (工具类)。

## 2. 数据库与资源连接管理
- **连接释放与池化**：在使用 SQLAlchemy、PyMongo 等 ORM/数据库引擎时，必须使用连接池。
- **依赖注入**：在 FastAPI 等支持依赖注入的框架中，必须使用 Dependency Injection (`Depends()`) 来获取并自动释放数据库 Session，严禁在路由函数内手动开关全局连接。

## 3. 异步编程范式
- **避免阻塞**：如果使用了 `async def` 定义路由，严禁在其中执行耗时的同步 I/O 操作（如阻塞式网络请求、繁重文件读写），必须使用相应的 `aio` 库（如 `aiohttp`, `aiofiles`）或将阻塞操作扔进线程池执行。
