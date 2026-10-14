# TaskForge

TaskForge 面向个人开发者和小型项目，通过 Python 接口和 CLI 提供持久化的异步任务执行。
目标运行方式是单机、单个 Worker 进程、多个异步任务并发执行。

**T01—T10 已实现**：可注册任务，并通过 Python 和 CLI 提交、查询及运行单个 Worker，
以有界异步并发执行、持久化有限重试、协作停机并恢复崩溃遗留尝试，保存状态与历史。
本地数据同步、下载备份、维护、故障恢复及业务幂等示例见 [场景指南](docs/scenarios.md)。
当前验收环境为 Linux/CPython 3.12；macOS、更高 Python 版本与全新联网依赖安装尚未验收。
包版本 `0.1.0` 用于本地构建与验收，尚未发布。

## 运行本地场景

先完成下文环境同步，在项目根目录执行：

```bash
uv run --locked python examples/verify_scenarios.py --output /tmp/taskforge-t10.json
```

不需要第三方凭据。脚本启动仅监听 loopback 的本地 API 替身，以真实 Python 和安装后的
`taskforge` CLI 完成提交、执行、分页与历史查询，再验证失败重试、超时清理、SIGINT/SIGTERM
停机与 SIGKILL 恢复。成功退出码为 0；临时数据库、文件和全部子进程自动清理，保留指定的
JSON 收据。省略 `--output` 时收据写到 stdout。

收据保留稳定 Job ID、每次 Attempt、Worker 日志、实际退出码、源码指纹和副作用记录。
在副作用已提交而 SUCCESS 尚未 commit 时强制退出，两组任务均真正执行两次：
普通业务产生两条副作用、合计 14；以 Job ID 为唯一键的业务只产生一条、合计 7。
已有实测收据见 [T10 场景证据](docs/verification-t10.json)，完整检查结果见
[验证记录](docs/verification.md)。幂等逻辑由示例业务实现，核心不提供恰好一次保证。

示例随 sdist 分发；wheel 提供核心库与 CLI，任务代码需要自行安装或放入 `PYTHONPATH`。

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
同步后提供 `taskforge` 命令入口，也可运行 `uv run --locked python -m taskforge.cli --help`。

## 质量检查与构建

```bash
uv run --locked --extra github ruff check .
uv run --locked --extra github ruff format --check .
uv run --locked --extra github mypy --strict src/taskforge integrations
uv build
```

构建生成 `dist/` 下的 wheel 和源码分发包；wheel 包含 `src/taskforge` 及 `py.typed`。

运行注册、校验、策略、显式加载与真实 SQLite 持久化及原子领取完成的行为测试：

```bash
uv run --locked --extra github pytest
```

T02—T10 已加入实际行为测试，包括真实 SQLite、事务故障、单次执行、协作取消、并发日志上下文、
独立 Worker/CLI 子进程之间的锁互斥、重复信号停机、持久化重试、强制退出与恢复的原子状态。

## GitHub 接入基础

G01 提供 `integrations/github/` 中的严格配置、认证、HTTP、独立业务存储和锁基础。
采集器、`github.refresh` 注册表和业务命令入口由 G02—G04 实现；七来源真实验收留给 G05。
默认七来源见 `integrations/github/default-sources.json`，配置与输入不保存凭据。

在项目根目录同步可选依赖：

```bash
uv sync --locked --extra github
```

业务使用源码运行并进入 sdist，wheel 继续仅分发核心与 CLI。pytest 配置将项目根目录
加入导入路径；运行独立脚本或后续 Worker 时，业务模块须可通过 `PYTHONPATH` 找到。
默认业务目录是 `.taskforge/github/`，业务数据库为 `business.sqlite`，独立于核心队列。

`GitHubConfig.load()` 加载默认配置或指定 JSON 文件；`RefreshInput` 保存稳定 UUID、规范化
目录、来源与预算快照、来源代次和操作。`BusinessStore.initialize()` 显式建立 schema v1，
每次读写独立连接，`apply(BatchWrite)` 原子保存记录、检查点与完成标记；网络在事务外执行。
来源变更须 `register_source(..., rebaseline=True)`，预算调整保留原检查点。

`resolve_credential()` 依次读取 GH_TOKEN、GITHUB_TOKEN、`gh auth token`。
`GitHubHTTP` 固定 GitHub API 根地址和版本 `2026-03-10`，提供 GET 与完整分页，
每次尝试共用 `AttemptBudget`，持久缓存和冷却跨客户端、进程生效，不额外自动重试。
生产沿用代理与证书环境，测试使用 MockTransport、假凭据和 `trust_env=False`。

来源锁必须包住存储读取、HTTP 客户端生命周期、业务提交及后续导出；嵌套客户端须先关闭，
再退出来源锁。数据库与 HTTP 操作及凭据子进程在重复取消时仍等待实际结束，锁文件保留。
实际检查、沙箱限制和交接见 [验证记录](docs/verification.md)。

## CLI

所有命令用 `--database PATH` 指定本地 SQLite 文件，默认 `taskforge.sqlite`；路径按
`DatabaseConfig` 展开并规范化，父目录须已存在。先显式 `init`，提交、查询和 Worker
不会自动为未初始化数据库建表。各命令的 `--help` 不加载任务模块或执行用户函数。

先保存下文的 `tasks.py`，在它所在目录执行。控制台入口通过 Python 的模块搜索路径加载
可信任务模块；本地单文件模块可显式设置 `PYTHONPATH="$PWD"`，已安装模块无需设置。

```bash
uv run --locked taskforge --help
uv run --locked taskforge init --database jobs.sqlite
printf '%s\n' '{"count":"3"}' | PYTHONPATH="$PWD" uv run --locked taskforge submit demo.count \
  --registry tasks:registry --database jobs.sqlite
uv run --locked taskforge list --database jobs.sqlite --limit 10
```

将 submit 输出的 UUID 保存为 `JOB_ID` 后，在一个终端启动 Worker，在另一个终端查询。
任务完成后向 Worker 发送 Ctrl-C 或 SIGTERM；Worker 持续轮询，不在队列空时自动退出。

```bash
PYTHONPATH="$PWD" uv run --locked taskforge worker --registry tasks:registry \
  --database jobs.sqlite --concurrency 4 --poll-interval-seconds 1 \
  --shutdown-grace-seconds 30
uv run --locked taskforge show "$JOB_ID" --database jobs.sqlite
uv run --locked taskforge history "$JOB_ID" --database jobs.sqlite
```

| 命令 | 参数与标准输出 |
| --- | --- |
| `init` | JSON 对象，含规范化 `database_path` 和 `initialized: true`；已有 schema 只校验 |
| `submit TASK_NAME --registry module:attribute` | `--input PATH` 读取 UTF-8 JSON 文件，默认 `-` 读取 stdin；输出一行已提交的 Job UUID |
| `worker --registry module:attribute` | 并发默认 4，轮询默认 1 秒，宽限默认 30 秒；配置复用 `WorkerConfig`；框架日志输出到 stderr |
| `list` | `--limit` 为 1—1000，默认 100；`--after` 接收上一页 `next_cursor` 字符串；输出 `{"jobs": [...], "next_cursor": "..."}` |
| `show JOB_ID` | 完整 Job JSON 快照，含参数 JSON 字符串、状态、策略、时间与错误摘要 |
| `history JOB_ID` | `{"job_id": "...", "attempts": [...]}`，按 number 升序；已有但未领取的 Job 返回空数组，未知 ID 报错 |

输出时间为带 UTC 时区的 ISO 8601 字符串，枚举使用其字符串值，空值为 JSON null。
list 的 `next_cursor` 是含 `created_at` 和 `id` 的 JSON 字符串，原样作为一个参数传给
`--after`；边界排他。非空页总是返回末项游标，空页返回 null；游标不保证后面还有记录。
show/history/list 无需 `--registry`，即使原任务模块已删除或无法导入仍可查询。
查询输出包含持久参数与错误详情；Worker 框架日志默认不输出参数或捕获完整 stdout/stderr。
用户函数自行 print 的内容仍写到自身 stdout，CLI 不拦截它。

参数语法错误由 Typer 输出帮助诊断，退出码 2；操作错误以单行 JSON 输出到 stderr，含
`error`、`message`、`job_id`、`attempt_number`（未知凭据为 null），stdout 不输出成功结果。
Worker 的最后一行错误报告前可能有 JSON 事件日志。

| 退出码 | 含义 |
| --- | --- |
| 0 | 帮助、初始化、提交、查询成功；Worker 已完成停止与持久化 |
| 2 | 参数语法、JSON/文件输入、模型/配置、序列化或分页校验失败 |
| 3 | 未知任务名称或 Job ID |
| 4 | 注册表引用、导入或属性加载失败 |
| 5 | 未初始化或不兼容 schema |
| 6 | Worker 锁冲突或锁文件访问失败 |
| 7 | 数据库路径或存储操作失败 |
| 8 | Executor 或其他框架/编程故障 |
| 9 | 提交、领取或完成的 commit 结果不确定；报告含可查询凭据 |
| 130 | 用户任务自行取消或运行协程取消，清理与中断持久化后传播 |

业务 TASK_ERROR/TIMEOUT 与 CONFIG_ERROR 按核心契约写入 Job/Attempt；它们本身不使
Worker 退出，最终状态用 show/history 查询。SIGINT/SIGTERM 只调用 `request_stop`；
重复信号不再取消用户清理。宽限期到时请求一次协作取消；用户不退出时 stderr 报告
`shutdown_incomplete`，进程继续等待、占槽持锁，尚未返回退出码 0。
结果不确定时先用 show/history 核对 `job_id` 与 `attempt_number`，CLI 不自动重交、
重新执行用户函数或判断副作用是否完成。SIGKILL 之后的恢复复用 Worker 原有流程。

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
  `validate_json(payload)` 从 JSON 字符串重新校验。提交端与 Worker 复用这些边界。
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
T03 将这些字段保存为数据库快照；T05 按快照执行超时，T07 按快照决策重试和退避。

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
| await store.finish_attempt(job_id, number, outcome, *, status, next_run_at=None, retry_delay_seconds=None) | 同一事务结束 Attempt 并更新 Job；成功 commit 后返回不可变 Job |

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
本次完成时间，且必须仍有尝试预算。此接口保存调用方明确给出的重试安排。
也可提供 `retry_delay_seconds`：有限正数，拒绝布尔值和字符串，仅用于 RETRY_WAIT，
不能与 `next_run_at` 同时提供。存储以本次完成事务时间加延迟生成到期时间，
避免数据库锁等待耗尽退避间隔；延迟向上取整到微秒，最短 1 微秒，超出 datetime 范围时
到期时间封顶为 UTC `datetime.max`。原有绝对时间接口仍严格校验未来时间。
存储不决定是否重试，也不计算指数退避；Worker 负责这些决策。

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

## 单次执行与超时

`Executor(registry)` 提供 `await executor.execute(claim) -> AttemptOutcome`，只接受
`claim_next()` 已成功提交的 `AttemptClaim`。执行前按任务名称查找定义，以用户模型再次校验
`claim.job.params_json`，再将模型作为一个位置参数传入 async 函数。超时读取
`claim.job.timeout_seconds` 快照，使用事件循环的单调计时器；注册默认策略变更不影响已有 Job。

| 执行情况 | 返回或传播 |
| --- | --- |
| 正常返回 `None` | `AttemptOutcome(AttemptStatus.SUCCESS)`，无错误字段 |
| 用户函数抛出 `Exception`，包括自行抛出的 `TimeoutError` | FAILED / TASK_ERROR，含异常类型、摘要和保留异常链的 traceback |
| 未知任务、JSON 无法按模型校验 | FAILED / CONFIG_ERROR，不调用用户函数 |
| 框架计时器触发 | 请求取消，等待用户协程退出，返回 FAILED / TIMEOUT |
| 外部取消或用户自行取消 | 等待用户协程退出后传播 `CancelledError`；不返回中断结果 |
| 框架故障、用户返回非 `None` | 抛出 `ExecutorError`，保留异常链；日志类别为 EXECUTOR_ERROR |

执行器不领取、不写数据库、不计算重试；结果由调用方传给 `finish_attempt`，并明确选择
Job 状态。错误详情保留完整文本，由存储边界截断为类型 256、摘要 2000、traceback 16000
个字符。EXECUTOR_ERROR 不属于持久 `ErrorKind`；Worker 会停止领取，
无法可靠完成的尝试留待恢复，不能保存成业务或配置失败。模型 validator 的编程故障
（如 `TypeError`）、错误详情生成或执行机制故障同样属于框架错误。
用户函数自己抛出的 `ExecutorError` 仍属于 TASK_ERROR。

超时和取消依赖协作：取消只向用户协程请求一次，等待其 `finally` 和其他清理结束；
重复外部取消不会再次打断清理。清理期间的外部取消优先于超时，最终仍传播取消。
超时取消后的业务异常或正常返回不会改成 TASK_ERROR 或 SUCCESS；返回契约违规仍抛框架错误。
外部取消后的清理业务异常不替代取消。协程一直忽略取消时，执行接口保持等待，
不提前报告结束或允许调用方释放槽位；没有线程池或硬超时，阻塞事件循环的代码也会阻塞计时器。

标准库 `logging` 由调用方配置，不修改 root logger 或自动安装 handler。`JSONFormatter`
输出 `time`（UTC ISO 时间）、`level`、`event`、`job_id`、`attempt_number` 和
`error_kind`。执行器使用 `taskforge.executor` logger，记录 attempt_started、
attempt_finished、attempt_interrupted、executor_error，默认不记录参数、异常内容或 stdout/stderr。
`get_execution_context() -> ExecutionContext | None` 返回不可变的当前身份；
contextvars 保证并发隔离，所有退出路径恢复进入前上下文。用户任务可以用同一个 formatter
关联自己的同步 handler 日志；用户日志消息由用户控制。跨线程队列 handler 需由调用方在生产端
保存上下文；执行器自身事件已显式携带身份。

保存前文的 `tasks.py`，将下面示例保存为 `execute_once.py`，运行
`uv run --locked python execute_once.py`。示例关闭领取端连接后执行，并由调用方保存成功结果；
输出 count=3、SUCCESS，同时 stderr 输出可解析 JSON 日志：

```python
import asyncio
import logging
from pathlib import Path

from taskforge import (
    DatabaseConfig,
    Executor,
    JobStatus,
    JSONFormatter,
    SQLiteStore,
    TaskForge,
)
from tasks import registry


async def main() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JSONFormatter())
    logger = logging.getLogger("taskforge")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

    config = DatabaseConfig(database_path=Path("execution.sqlite"))
    async with SQLiteStore(config) as store:
        await store.initialize()
        await TaskForge(store, registry).submit("demo.count", {"count": 3})
        claim = await store.claim_next()
        assert claim is not None

    outcome = await Executor(registry).execute(claim)
    async with SQLiteStore(config) as store:
        done = await store.finish_attempt(
            claim.job_id, claim.number, outcome, status=JobStatus.SUCCESS
        )
        print(done.status.value)


if __name__ == "__main__":
    asyncio.run(main())
```

本例声明的用户函数正常返回；通用调用方需要分别处理失败结果、取消和框架错误。
下面的 Worker 接口负责领取、单次执行和完成提交。

## Worker 与并发控制

`Worker(store, registry, config=None)` 使用调用方拥有的 `SQLiteStore` 和显式注册表。
数据库须先显式初始化，存储须保持打开直到 `run()` 返回。`WorkerConfig` 不可变：
`concurrency=4` 为正整数，`poll_interval_seconds=1.0` 为有限正数；拒绝布尔值和字符串。
`shutdown_grace_seconds=30.0` 为有限非负数；默认给运行任务 30 秒自然结束，0 表示立即
请求协作取消，同样拒绝布尔值和字符串。宽限期使用事件循环单调时钟，不是硬超时。

`await worker.run()` 先取得规范化数据库路径加 `.lock` 的非阻塞独占文件锁，
再检查现有 schema，完成遗留 RUNNING 恢复，启动固定数量的异步槽位。
每个槽位覆盖领取、用户执行及完成 commit；
用户代码运行时没有存储会话。空槽位每隔轮询间隔检查队列，不忙等。
成功保存 SUCCESS；业务异常和框架超时按 Job 快照的剩余预算保存 RETRY_WAIT 或最终 FAILED。
配置错误直接保存 FAILED。

`worker.request_stop()` 同步停止新领取并唤醒空闲槽位；等待已启动工作，宽限期后请求取消
用户执行。已开始的数据库操作继续到明确结束。每个 Worker 实例只能 run 一次。
以下示例保存为 `worker_once.py`，
复用前文的 `tasks.py`，运行 `uv run --locked python worker_once.py`，打印 `SUCCESS`：

```python
import asyncio
from pathlib import Path

from taskforge import DatabaseConfig, JobStatus, SQLiteStore, TaskForge, Worker, WorkerConfig
from tasks import registry


async def main() -> None:
    config = DatabaseConfig(database_path=Path("worker.sqlite"))
    async with SQLiteStore(config) as store:
        await store.initialize()
        job = await TaskForge(store, registry).submit("demo.count", {"count": 3})
        worker = Worker(store, registry, WorkerConfig(concurrency=2, poll_interval_seconds=0.05))
        run = asyncio.create_task(worker.run())
        try:
            while not run.done():
                if (await store.get(job.id)).status in (JobStatus.SUCCESS, JobStatus.FAILED):
                    break
                await asyncio.sleep(0.05)
        finally:
            worker.request_stop()
            await run
        print((await store.get(job.id)).status.value)


if __name__ == "__main__":
    asyncio.run(main())
```

同一数据库第二个 Worker 启动抛 `WorkerLockError`，不会进入 schema 检查、恢复或领取；不同数据库独立。
锁文件不删除，描述符直到全部已启动工作结束才关闭。数据库故障、结果不确定或
`ExecutorError` 立即停止新领取，按相同停机流程等待其他已启动任务结束后传播原异常及其 cause；
不自动重交或再次执行。`taskforge.worker` 的 `worker_error` 日志使用 JSONFormatter，
类别为 STORAGE_ERROR、EXECUTOR_ERROR、LOCK_ERROR 或 WORKER_ERROR；有已知凭据时
携带 job_id 和 attempt_number。领取前故障没有任务身份，记录 null。
日志类别不扩展持久 ErrorKind，不默认输出参数或异常内容。

`ClaimUncertainError` / `CompletionUncertainError` 携带 job_id、number，调用方须在
run 结束后用 `store.get(error.job_id)` 与 `store.history(error.job_id)` 查询确认；
不确定领取不会执行用户函数，不确定完成不会再次执行。无法可靠完成的尝试保持 RUNNING。
恢复完成的 commit 不确定性也使用 CompletionUncertainError；先查询确认再决定后续行动。

## 持久化重试

`max_retries=N` 表示最多 N 次额外尝试，总次数不超过 N+1；领取成功即消耗一次预算。
TASK_ERROR（包括用户自行抛出的 TimeoutError）、框架 TIMEOUT 和 INTERRUPTED 有预算时重试，
CONFIG_ERROR、成功、ExecutorError 和存储故障不会触发自动重执行。

第 k 次失败后的等待为 `min(retry_cap_seconds, retry_base_seconds × 2^(k−1))`，
首期无抖动。内部 `policy.decide_completion(job, outcome)` 是纯函数，返回不可变
`CompletionDecision(status, retry_delay_seconds)`；`policy.retry_delay_seconds(job)`
使用有界浮点指数计算，支持很大的尝试次数和全部正有限浮点策略值。
策略只来自提交时的 Job 快照，更换注册表默认值不改变已有 Job。

完成事务同时保存失败 Attempt、RETRY_WAIT 和 next_run_at，成功 commit 后释放槽位。
空槽位继续轮询数据库；重试等待不占用执行槽位，不靠长时间内存 sleep 保存计划。
领取边界包含到期时刻，实际执行可能因轮询间隔或槽位繁忙稍晚。
Worker 重启保留原到期时间，普通等待 Job 不会提前执行。

每次重试保持 Job ID、参数、策略和首次 started_at，创建新的递增 Attempt。
最终成功清空 Job 当前错误摘要，保留此前失败的类型、摘要与 traceback。
重试可能重复业务副作用，任务须自行保证幂等。

## 停机与崩溃恢复

取得独占锁才说明旧 Worker 已退出。Worker 在 schema 校验后逐个读取遗留 RUNNING
Job，通过既有 `finish_attempt` 在一个短事务内将当前 Attempt 保存为 INTERRUPTED，
按 Job 策略快照的剩余预算保存 RETRY_WAIT 或 FAILED，然后才开始正常领取。
`SQLiteStore.get_running_attempt() -> AttemptClaim | None` 只读取恢复凭据，不授予所有权；
恢复调用方须持有 WorkerLock，并持锁直到完成提交。Worker 已负责这个顺序。

恢复不新增 Attempt、不增加次数，保留 ID、参数、策略、首次 started_at 与此前历史。
Attempt.finished_at 表示确认中断的事务时间，实际崩溃时刻未知；退避从该时间计算。
已在 RETRY_WAIT 的任务保留原 next_run_at，终态不重启。恢复期间再次退出时，已提交 Job
不会重复中断，未提交 Job 仍为 RUNNING，下次启动继续恢复。

停机宽限期过后只取消执行器，不取消领取、恢复或完成事务。用户协程实际结束后才保存
INTERRUPTED、释放槽位；有预算仍安排持久化重试，本次停机不再领取。
若宽限期已结束但先前领取尚未返回，该领取继续确认 commit；成功后直接保存中断，
不开始用户函数。正常完成及已经开始的完成写入保留原结果。

外部取消 `run()` 使用同一停机流程，等待用户清理及写入结束后传播 CancelledError；
重复取消不会再次打断清理。用户自行取消也在结束后保存中断、停止领取并传播取消。
宽限期到时仍有执行任务，`taskforge.worker` 记录 WARNING 事件 `shutdown_incomplete`，
含 job_id、attempt_number 和 INTERRUPTED；表示停机尚未完成，不表示已持久化中断。
用户忽略取消或清理一直不结束时，Worker 继续等待、占槽并持锁，不能返回成功停机。

外部副作用与 SQLite 成功状态无法原子提交。真实子进程测试在文件副作用完成、成功事务
尚未 commit 时强制退出；恢复后同一 Job 的第 2 次尝试再次写入文件，证明可能重复执行。
业务可使用稳定 Job ID 实现自己的幂等；框架不提供业务去重或恰好执行一次保证。

## 已确认的 MVP 语义

- 任务通过装饰器注册 `async` 函数，接收单个 Pydantic 输入模型。
- SQLite 是持久队列；任务状态与每次尝试历史分别存储。
- 每个任务保持稳定 ID，`max_retries=N` 最多产生 `N+1` 次尝试。
- 重试到期时间持久化；领取即消耗预算，异常退出后的恢复不另增次数。
- 独占进程锁限制同一数据库只运行一个 Worker。
- 外部副作用完成后、成功状态提交前崩溃，可能导致重复执行；业务任务需要自行实现幂等。
- 异步超时和取消依赖任务协作，不提供 CPU 执行池或硬超时保证。

详细设计见 [架构说明](docs/architecture.md)，开发顺序见 [迭代计划](docs/implementation-plan.md)，
实际检查结果见 [验证记录](docs/verification.md)。

T01—T10 每个任务由独立会话实现。T10 完成场景与当前环境验收后停止，验收后按用户追加要求提交改动。
其余平台的验收范围及历史交接记录保留在验证文档中。
