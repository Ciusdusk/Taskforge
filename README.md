# TaskForge

TaskForge 面向个人开发者和小型项目，计划通过 Python 接口和 CLI 提供持久化的异步任务执行。
目标运行方式是单机、单个 Worker 进程、多个异步任务并发执行。

**T01：工程骨架已验收完成**，尚未实现任务注册、提交、执行或 CLI。
包版本 `0.1.0` 用于开发阶段的构建验证，不代表 MVP 已完成或已发布。

## 环境准备

需要 Python 3.12+ 和 uv。首期运行与故障恢复验证面向 Linux/macOS 的本地文件系统。
`.python-version` 将开发环境选在 Python 3.12；更高版本的兼容性尚未验证。

在项目根目录执行：

```bash
uv sync --locked
uv run --locked python -c 'import taskforge; print(taskforge.__file__)'
```

运行依赖为 Pydantic 2、SQLAlchemy 2.0、aiosqlite 和 Typer；开发依赖为 pytest、
pytest-asyncio、Ruff 和 mypy。`uv.lock` 固定具体版本，`uv sync --locked` 安装运行与开发依赖。
T01 已使用完整本机缓存完成离线验收，见 [验证记录](docs/verification.md)。
尚未提供 CLI 入口，因此当前不能运行 `taskforge submit` 或 `taskforge worker`。

## 质量检查与构建

```bash
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
uv build
```

构建生成 `dist/` 下的 wheel 和源码分发包；wheel 包含 `src/taskforge` 及 `py.typed`。

从 T02 加入首批行为测试后执行：

```bash
uv run --locked pytest
```

T01 没有测试用例。当前运行 pytest 会因未收集到测试而返回退出码 5，不能将其报告为测试通过。
关键并发与恢复测试将使用临时 SQLite 文件和真实子进程，不以占位测试代替验证。

## 已确认的 MVP 语义

- 任务通过装饰器注册 `async` 函数，接收单个 Pydantic 输入模型。
- SQLite 是持久队列；任务状态与每次尝试历史分别存储。
- 每个任务保持稳定 ID，`max_retries=N` 最多产生 `N+1` 次尝试。
- 重试到期时间持久化；异常退出后的恢复消耗现有尝试预算。
- 独占进程锁限制同一数据库只运行一个 Worker。
- 外部副作用完成后、成功状态提交前崩溃，可能导致重复执行；业务任务需要自行实现幂等。
- 异步超时和取消依赖任务协作，不提供 CPU 执行池或硬超时保证。

详细设计见 [架构说明](docs/architecture.md)，开发顺序见 [迭代计划](docs/implementation-plan.md)，
实际检查结果见 [验证记录](docs/verification.md)。

T01—T10 每个任务由一个独立会话实现。T01 验收完成后，使用 [T02 新会话 prompt](docs/prompts/T02.md)
进入任务注册与输入校验。
