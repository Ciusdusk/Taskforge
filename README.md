# TaskForge

TaskForge 面向个人开发者和小型项目，计划通过 Python 接口和 CLI 提供持久化的异步任务执行。
目标运行方式是单机、单个 Worker 进程、多个异步任务并发执行。

**T01—T04 已验收完成**：可注册任务、校验输入、持久化提交和查询、原子领取和完成尝试；Worker 与 CLI 尚未实现。
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

运行注册、校验、策略、显式加载与真实 SQLite 持久化及原子领取完成的行为测试：

```bash
uv run --locked pytest
```

T02—T04 已加入实际行为测试，包括临时 SQLite 文件、并发提交与领取、完成身份校验和事务故障。
后续 Worker 与恢复测试将使用真实子进程。

## 任务注册与输入校验

将以下内容保存为 `tasks.py`，运行 `uv run --locked python tasks.py`，输出 `count=3`：

```python
import asyncio

from pydantic import BaseModel, ConfigDict

from taskforge import ExecutionPolicy, TaskRegistry


class CountInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    count: int


registry = TaskRegistry()


@registry.task(
    "demo.count",
    input_model=CountInput,
    policy=ExecutionPolicy(max_retries=2),
)
async def count(data: CountInput) -> None:
    print(f"count={data.count}")


if __name__ == "__main__":
    definition = registry.get("demo.count")
    payload = definition.serialize_input({"count": "3"})
    restored = definition.validate_json(payload)
    asyncio.run(count(restored))
```

装饰器返回原函数，仍可直接 `await count(CountInput(count=3))`。
本例只演示直接调用；策略此时只作为注册定义保存。
显式加载同一文件中的注册表：

```bash
uv run --locked python -c 'from taskforge import load_registry; print(load_registry("tasks:registry").get("demo.count").name)'
```

公共接口从 `taskforge` 导入：

- `TaskRegistry.task(name, *, input_model, policy=None)` 注册接收单个位置参数的 async 函数；
  `get(name)` 按稳定名称查找 `TaskDefinition`，不同注册表互相隔离。
- `TaskDefinition` 保存 `name`、`function`、`input_model`、`policy`；
  `validate_input(data)` 校验 Python 输入，`serialize_input(data)` 生成 JSON 字符串，
  `validate_json(payload)` 从 JSON 字符串重新校验。后续提交端与 Worker 复用这些边界。
- `load_registry("module:attribute")` 导入指定模块并读取注册表属性；模块是本地可信代码。

`input_model` 必须是具体 Pydantic 对象模型，不能是 `BaseModel` 本身或 `RootModel`，
以满足持久参数为 JSON 对象的契约。函数若提供输入及返回注解，必须分别与显式模型和 `None` 一致；
字符串注解须能在函数的定义环境解析。不接受同步函数、async generator、额外参数、
仅关键字参数或可变参数。函数遵守返回 `None` 的契约，注册阶段不会执行用户函数。
任务名称为非空字符串且不含首尾空白；重名注册被拒绝，失败不替换已有项。

模型自行决定类型转换、严格模式和未知字段的处理；框架不修改模型配置。
JSON 使用别名及 Pydantic `round_trip` 序列化，再用同一模型检查可重新校验性。
`validate_json` 使用 JSON 校验语义，例如严格模式下仍可读取 JSON 中的 datetime 字符串。
自定义模型需保证序列化表示可重新校验；框架不保证任意有损 serializer 的值完全相等。

非法输入或执行策略抛出 Pydantic `ValidationError`。框架错误继承 `TaskForgeError`：
`RegistrationError` 表示非法注册声明，`UnknownTaskError` 表示未注册的名称，
`RegistryLoadError` 表示加载失败，`InputSerializationError` 表示无法生成可重新校验的 JSON 对象。
底层导入或序列化错误保留在异常链中。

`ExecutionPolicy` 不可变，注册时重新校验并保存独立副本，默认值如下：

| 字段 | 默认值 | 约束与理由 |
| --- | --- | --- |
| `max_retries` | `0` | 非负整数，拒绝布尔值及字符串；默认避免自动重复业务副作用 |
| `retry_base_seconds` | `1.0` | 有限正数；为后续重试提供短暂初始等待 |
| `retry_cap_seconds` | `60.0` | 有限正数且不小于基数；限制后续退避等待增长 |
| `timeout_seconds` | `300.0` | 有限正数；为常见异步 I/O 提供五分钟的初始预算 |

时间接受数值，拒绝布尔值和字符串；以上只是起始默认值，用户可声明任务自己的策略。
T03 将这些字段保存为数据库快照；执行器、退避调度和超时执行尚未实现。

## 持久化提交与查询

先保存上面的 `tasks.py`，再将以下内容保存为 `submit_query.py`，执行
`uv run --locked python submit_query.py`。示例创建当前目录的 `taskforge.sqlite`，提交一条 Job，
关闭连接后重新打开并查询。任务函数不会在提交时执行；每次运行都会提交新的 Job。

```python
import asyncio
from pathlib import Path

from taskforge import DatabaseConfig, SQLiteStore, TaskForge
from tasks import registry


async def main() -> None:
    config = DatabaseConfig(database_path=Path("taskforge.sqlite"))
    async with SQLiteStore(config) as store:
        await store.initialize()
        app = TaskForge(store, registry)
        job = await app.submit("demo.count", {"count": "3"})
        print(job.id, job.status.value, job.params_json)

    async with SQLiteStore(config) as store:
        query = TaskForge(store)
        restored = await query.get(job.id)
        assert restored == job
        page = await query.list(limit=10)
        print(f"jobs={len(page)}, attempts={len(await query.history(job.id))}")
        if page:
            following = await query.list(limit=10, after=page[-1].cursor)
            print(f"next_page={len(following)}")


if __name__ == "__main__":
    asyncio.run(main())
```

公共接口均从 `taskforge` 导入：

| 接口 | 返回值与行为 |
| --- | --- |
| `DatabaseConfig(database_path=..., busy_timeout_ms=5000)` | 不可变配置；路径展开并规范化，必须指向本地文件，父目录须已存在 |
| `SQLiteStore(config)` | async context manager；退出时 `close()` 释放连接，也可显式调用 |
| `await store.initialize()` | 原子建表并以 `PRAGMA user_version=1` 记录版本；重复调用保留数据 |
| `TaskForge(store, registry=None)` | 提交需要显式注册表；查询不需要导入任务模块 |
| `await app.submit(task_name, data)` | 返回不可变 `Job`；先查任务并按 T02 契约校验、序列化，成功表示 commit 完成 |
| `await app.get(job_id)` | 返回持久 `Job` 快照；不存在（含格式异常的字符串 ID）抛出 `UnknownJobError` |
| `await app.list(limit=100, after=None)` | 返回 `tuple[Job, ...]`，按 `(created_at, id)` 升序；空页返回 `()` |
| `await app.history(job_id)` | 返回按 `number` 升序的 `tuple[Attempt, ...]`；已存在且未领取的 Job 返回 `()`，不存在抛出 `UnknownJobError` |
| `SQLiteStore.submit/get/list/history` | 相同存储行为；直接提交接收 `TaskDefinition` 和输入对象 |

`limit` 必须是 1—1000 的整数，拒绝布尔值。`after` 为上一页末项的 `job.cursor`，
或 `JobCursor(created_at, id)`：时间必须带时区，ID 必须是规范 UUID4 字符串；
参数错误抛出 `PaginationError`。游标位置为排他边界，不要求对应记录仍存在。
每页使用独立事务，分页期间的新提交可能出现在后续页；不承诺跨页一致性快照。

`Job` 包含架构中的全部 Job 字段，策略字段直接保存在快照上；`JobStatus` 枚举表示状态。
初始状态为 `PENDING`，`attempt_count=0`、`next_run_at=created_at`，开始、结束与错误字段为空。
`Attempt` 包含全部尝试字段，使用 `AttemptStatus` 与 `ErrorKind` 枚举。
返回模型为 frozen dataclass，列表为元组，参数为已校验 JSON 字符串；不会泄露 ORM 或会话。
数据库时间使用 UTC epoch 微秒整数，对外为 UTC 带时区 `datetime`，不经过浮点时间戳转换。
更改任务默认策略只影响后续提交。

初始化是显式操作，已有兼容数据库重开后可直接查询。未初始化、版本不兼容、
版本为 1 却缺少表或列不匹配时，初始化或操作明确报错；`SchemaError` 继承 `StorageError`。
拒绝接管有表但无版本的数据库；不提供迁移，也不自动修复人为修改的 schema。
SQLite 的状态、整数次数、预算、正有限时间、退避范围、外键及索引约束由数据库执行。
错误摘要最多 2000 个字符，异常类型最多 256 个字符，traceback 最多 16000 个字符；
直接超限写入被约束拒绝；finish_attempt 在保存错误文本前截断到这些上限。

每次操作使用独立短会话；查询开启普通事务，写入与建表统一使用 `BEGIN IMMEDIATE`。
每个连接开启外键，锁等待由 `busy_timeout_ms` 限制（允许 0—2147483647 的整数毫秒）。
写入失败回滚并抛出 `StorageError`，底层异常保留在异常链中。
commit 失败时使连接失效并关闭，防止残留事务被连接池复用。
如果 commit 已开始但未得到明确结果，抛出带 `job_id` 的 `SubmissionUncertainError`，
调用方应重开存储并 `get(error.job_id)` 确认后再决定是否重新提交。
commit 阶段的协程取消也转为该错误，原 `CancelledError` 保留在异常链中；
commit 前取消仍传播取消，并回滚。这里不提供自动重交或业务去重。

## 原子领取与完成

SQLiteStore 提供以下接口，不要求注册表，也不执行用户任务函数：

| 接口 | 返回值与行为 |
| --- | --- |
| await store.claim_next() | 返回不可变 AttemptClaim；没有可领取任务时返回 None |
| claim.job / claim.job_id / claim.number | 已提交的 RUNNING Job 快照、稳定 ID 与本次尝试序号；后两项组成完成凭据 |
| AttemptOutcome(status, error_kind=None, exception_type=None, error_message=None, traceback=None) | 不可变单次结果，字段在 finish_attempt 边界校验 |
| await store.finish_attempt(job_id, number, outcome, *, status, next_run_at=None) | 同一事务结束 Attempt 并更新 Job；成功 commit 后返回不可变 Job |

领取使用一个 BEGIN IMMEDIATE 事务，筛选 PENDING 或 RETRY_WAIT、next_run_at 不为空且已到期、
attempt_count < max_retries + 1 的 Job，按 (next_run_at, created_at, id) 升序取首项。
条件 UPDATE 再次检查状态、到期时间和预算，变为 RUNNING、计数加一并插入对应 RUNNING Attempt。
首次设置 Job.started_at，重试保留首次时间；清空 next_run_at 和 finished_at，保留参数、策略
和上次错误摘要。时间在获得写事务后读取，到期边界包含相等时间。

完成要求 Job 为 RUNNING、number 等于 attempt_count 且对应 Attempt 仍为 RUNNING。
未知 Job 抛 UnknownJobError；错误序号、错误 Job、过期或重复完成抛 AttemptConflictError，
不会覆盖记录。number 必须是正整数，拒绝布尔值及浮点数。
结果组合、字段类型或重试时间非法时抛 InvalidAttemptResultError，并保留原状态。

| AttemptOutcome.status | error_kind | 可选 Job status |
| --- | --- | --- |
| AttemptStatus.SUCCESS | 必须为空，全部错误字段也必须为空 | JobStatus.SUCCESS |
| AttemptStatus.FAILED | TASK_ERROR、TIMEOUT 或 CONFIG_ERROR | JobStatus.FAILED；非 CONFIG_ERROR 也可 RETRY_WAIT |
| AttemptStatus.INTERRUPTED | 必须为 INTERRUPTED | JobStatus.FAILED 或 RETRY_WAIT |

结果和状态必须使用枚举；RUNNING 不是完成结果。失败时 error_kind 必填，其他错误字段可为空。
异常类型、摘要、traceback 必须是字符串或 None；分别按 Python 字符截取前 256、2000、16000
个字符，不修改调用方的 outcome。Job 当前摘要与本次 Attempt 相同；成功清空当前错误，
已有失败 Attempt 的详情保持不变。

Attempt.finished_at 使用本次完成事务的 UTC 时间。终态 Job.finished_at 与之相同且
next_run_at 为空；RETRY_WAIT 的 Job.finished_at 为空，next_run_at 必须带时区、严格晚于
本次完成时间，且必须仍有尝试预算。此接口保存调用方明确给出的重试安排；
重试决策与退避计算留在 T07。

先保存前文的 tasks.py，再将以下示例保存为 claim_finish.py，运行
uv run --locked python claim_finish.py。示例使用 attempts.sqlite 提交一条任务，
手动提供成功结果来演示存储接口，输出 RUNNING SUCCESS 1：

```python
import asyncio
from pathlib import Path

from taskforge import (
    AttemptOutcome,
    AttemptStatus,
    DatabaseConfig,
    JobStatus,
    SQLiteStore,
    TaskForge,
)
from tasks import registry


async def main() -> None:
    config = DatabaseConfig(database_path=Path("attempts.sqlite"))
    async with SQLiteStore(config) as store:
        await store.initialize()
        await TaskForge(store, registry).submit("demo.count", {"count": 3})
        claim = await store.claim_next()
        assert claim is not None
        done = await store.finish_attempt(
            claim.job_id,
            claim.number,
            AttemptOutcome(AttemptStatus.SUCCESS),
            status=JobStatus.SUCCESS,
        )
        print(claim.job.status.value, done.status.value, len(await store.history(done.id)))


if __name__ == "__main__":
    asyncio.run(main())
```

领取和完成复用短会话与 _commit；commit 前的数据库故障抛 StorageError 并完整回滚，
底层异常保留在异常链中。commit 前取消传播 CancelledError 并回滚。
commit 开始后的故障或取消分别抛 ClaimUncertainError / CompletionUncertainError，
均携带 job_id 和 number；原始错误留在异常链中，失败连接会失效。
不确定领取不返回可执行凭据；调用方应停止执行该领取，使用 get/history 核对持久状态。
不确定完成也须先查询，不能直接认定失败或重新执行任务。这里不自动重领或重交结果。

T04 尚无 Executor、Worker、重试决策与调度、进程锁、恢复、日志系统或 CLI。

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

T01—T10 每个任务由一个独立会话实现。下一会话使用 [T05 交接 prompt](docs/prompts/T05.md)
实现单次执行与超时；T04 验收后按用户追加要求提交改动。
