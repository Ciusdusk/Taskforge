# TaskForge

TaskForge 面向个人开发者和小型项目，计划通过 Python 接口和 CLI 提供持久化的异步任务执行。
目标运行方式是单机、单个 Worker 进程、多个异步任务并发执行。

**T01 工程骨架与 T02 任务注册、输入校验已验收完成**，尚未实现持久化提交、Worker 或 CLI。
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

运行注册、校验、策略与显式加载的行为测试：

```bash
uv run --locked pytest
```

T02 已加入实际行为测试。后续并发与恢复测试将使用临时 SQLite 文件和真实子进程。

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
本阶段没有数据库、提交 API、执行器、退避调度或超时执行。

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

T01—T10 每个任务由一个独立会话实现。下一会话使用 [T03 交接 prompt](docs/prompts/T03.md)
实现持久化提交与查询。
