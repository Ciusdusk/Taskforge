# 实际验证记录

## T01：工程骨架

验证日期：2026-10-13。环境：Linux x86_64、uv 0.10.9、项目虚拟环境 CPython 3.12.13。
系统 `python3` 为 3.12.3；uv 选用了本机已存在的 3.12.13，没有下载 Python。

**状态：T01 已完成。** 完整验收及清理后的复核见下文，`verification-t01.json` 保留原验收证据。
以下保留此前失败的历史记录。
未实现业务模块，也未创建 Git Commit。

### 首次依赖同步与环境限制

执行 `uv sync --cache-dir /tmp/taskforge-t01-uv-cache --no-python-downloads` 返回退出码 2：
PyPI 请求失败，网络连接被沙箱限制。扩大沙箱权限的请求被当前执行策略拒绝，未获得额外权限。

将本机已有缓存复制到临时目录后，离线同步返回退出码 1：缓存中找不到 mypy。
完整运行依赖的离线安装也未成功：SQLAlchemy 的可用缓存不足以由该命令安装当前平台版本。
未降低依赖要求、移除 mypy，或生成不完整的锁文件。

当时为执行已有检查，仅离线安装了构建与检查工具，`.venv` 尚未完整同步。

工具安装命令：

```bash
uv pip install --offline --cache-dir /tmp/taskforge-t01-uv-cache \
  --link-mode copy --python .venv/bin/python \
  'pytest>=8,<10' 'pytest-asyncio>=1,<2' 'ruff>=0.6,<1' 'hatchling>=1.25,<2'
```

实际工具版本：pytest 9.1.1、pytest-asyncio 1.4.0、Ruff 0.15.11、Hatchling 1.29.0。
临时缓存已在检查完成后删除；未修改用户的全局缓存。

### 首次检查结果

下列检查使用 `--no-sync`，只证明已有工具与项目文件的检查结果，不代表完整依赖同步成功。

| 检查 | 实际命令或方法 | 结果 |
| --- | --- | --- |
| 构建 | `uv build --offline --cache-dir /tmp/taskforge-t01-uv-cache --no-python-downloads` | 退出码 0，生成 sdist，并从 sdist 构建 wheel |
| Ruff | `uv run --no-sync --offline --cache-dir /tmp/taskforge-t01-uv-cache ruff check .` | 退出码 0，All checks passed |
| 格式 | `uv run --no-sync --offline --cache-dir /tmp/taskforge-t01-uv-cache ruff format --check .` | 退出码 0，1 file already formatted |
| pytest 配置 | `uv run --no-sync --offline --cache-dir /tmp/taskforge-t01-uv-cache pytest` | 退出码 5，收集 0 个测试；严格配置无报错；不记为测试通过 |
| mypy | `uv run --no-sync --offline --cache-dir /tmp/taskforge-t01-uv-cache mypy` | 退出码 2，找不到可执行文件；未完成类型检查 |
| 产物结构 | 使用标准库 zipfile/tarfile 检查实际归档 | wheel 包含包与 py.typed；sdist 包含源码、配置、README 和架构说明 |
| wheel 导入 | 安装构建 wheel 后，从 `/tmp` 使用项目 Python 的 `-I` 模式导入 | 退出码 0；模块来自 .venv/site-packages，版本为 0.1.0，py.typed 存在 |
| Git 状态 | `git status --short` | 退出码 128；现有 .git 目录没有有效仓库元数据 |

### 首次检查后的待补验收

wheel 安装使用 `uv pip install --offline --cache-dir /tmp/taskforge-t01-uv-cache --no-deps
--python .venv/bin/python dist/taskforge-0.1.0-py3-none-any.whl`。该检查验证分发产物本身，
不验证运行依赖；当前包尚未使用这些依赖。

在能访问 PyPI 的环境运行：

```bash
uv sync
uv sync --locked
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
uv build
uv run --locked python -c 'import taskforge; print(taskforge.__file__)'
```

成功后保留生成的 `uv.lock`，更新本记录，才将 T01 标记完成。
T01 未添加占位测试；实际行为测试从 T02 开始。

### 网络复查与解决方法

2026-10-13 在相同执行环境复查：

- HTTP/HTTPS 代理已配置，连接代理返回 `PermissionError`，errno 为 1。
- 在单次请求中禁用代理后，PyPI DNS 解析返回 `gaierror`，errno 为 -3。
- 当前会话的权限策略禁止扩大沙箱权限。没有再次请求已经被拒绝的提权。
- 重新执行完整验收脚本，仍在 `uv sync` 阶段因代理 TCP 连接 `Operation not permitted` 失败。
  脚本返回退出码 1，未生成 `docs/verification-t01.json`。

以上结果表明当前执行环境不具备可用联网路径；无法据此判断宿主机代理或网络是否正常。
本轮未修改全局代理、DNS 或包索引设置。

此前使用一次性脚本完成验收。该脚本现已移除；如需重新检查，在能联网的宿主机终端中运行：

```bash
cd /home/xciusdusk/projects/personal/TaskForge
uv sync --locked
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
uv build
```

当时脚本依次执行依赖同步、锁文件同步、Ruff、格式检查、mypy、pytest 配置与测试、构建、归档检查
以及独立虚拟环境中的 wheel 导入。T01 的 pytest 退出码 5 单独记为未收集到测试，
不记为测试通过。其他必要检查失败即停止，只有整套验收成功才生成 JSON 记录。
临时 wheel 测试环境自动清理，运行依赖和开发依赖保持完整声明。

若宿主机也出现网络错误，先对比正常代理路径与直连路径：

```bash
curl -I --fail --silent --show-error --connect-timeout 5 --max-time 15 https://pypi.org/simple/mypy/
curl --noproxy '*' -I --fail --silent --show-error --connect-timeout 5 --max-time 15 https://pypi.org/simple/mypy/
```

仅当直连成功而代理路径失败时，可在该次依赖同步进程中移除代理环境变量：

```bash
env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
  -u http_proxy -u https_proxy -u all_proxy uv sync --locked
```

两种路径均失败时，需要先恢复宿主机的 DNS、代理或网络访问，或使用能访问包索引的执行环境。
本轮并未在宿主机运行这些命令，因此不报告网络已恢复。

### 交接辅助文件检查（历史）

新增 scripts/verify_t01.py 和 docs/prompts/T02.md，记录每个任务一个会话的工作方式。
验收脚本语法编译通过，Ruff 检查与格式检查通过；完整成功路径尚未在可联网环境执行。
补充构建使用 `uv build --offline --no-cache --no-build-isolation --python .venv/bin/python
--no-python-downloads`，退出码 0。实际归档检查确认源码包包含验收脚本及 T02 prompt，
wheel 仍包含包与 py.typed。网络重试临时缓存已清理。
该次检查后 T01 仍为部分完成，T02 未开始。

### T01 完整验收：通过

检查时已发现 `uv.lock`，运行依赖和 mypy 均已安装。使用项目现有完整缓存重新验收，
没有访问网络、升级依赖或修改全局配置。

以下为当时实际运行的命令，其中临时验收脚本现已移除：

```bash
uv sync --locked --offline --cache-dir .cache/uv --no-python-downloads
UV_OFFLINE=true UV_CACHE_DIR="$PWD/.cache/uv" python3 scripts/verify_t01.py
```

两条命令均返回退出码 0。实际检查结果：

| 检查 | 结果 |
| --- | --- |
| 依赖同步及 locked 同步 | 通过；解析 27 个包、审核 26 个安装包 |
| Ruff 检查及格式检查 | 通过 |
| mypy strict | 通过，1 个源码文件无问题 |
| pytest | 收集 0 个测试，退出码 5；按 T01 约定记录，不声称测试通过 |
| 构建 | sdist 和 wheel 构建通过 |
| 归档检查 | wheel 包含包与 py.typed；sdist 包含 uv.lock |
| 隔离 wheel 导入 | 在临时独立虚拟环境中从项目外成功导入，版本为 0.1.0 |
| 机器验收记录 | 生成 docs/verification-t01.json，仅在全部必要检查成功后写入 |

当前环境为 Python 3.12.13、mypy 1.20.2、Ruff 0.17.0、pytest 9.1.1、pytest-asyncio 1.4.0。
运行依赖为 aiosqlite 0.22.1、Pydantic 2.14.1、SQLAlchemy 2.0.54、Typer 0.27.3。
临时 wheel 虚拟环境由脚本自动清理。网络限制本身未宣称已解除。

项目新增 AGENTS.md，将全部代码生成与修改绑定到 karpathy-guidelines skill；T02 prompt 引用该约束。
本轮仅核验 T01 及更新约束和交接文档，T02 未开始。

### T01 验收工具清理

按用户要求删除已完成用途的 scripts/verify_t01.py，并清理 README、AGENTS.md、迭代计划、
T02 prompt 和源码分发配置中的使用要求。后续直接运行已有质量检查与构建命令。
历史记录中的脚本路径仅用于说明当时的验证过程，不再是可执行入口。

保留 docs/verification-t01.json 的原始内容；其中 pyproject.toml 的指纹属于清理前的历史状态，
不作为当前配置的校验门禁。

清理后的复核结果：locked 离线同步、构建、Ruff 检查、格式检查及 mypy 均返回退出码 0。
当前文档中的本地链接有效，使用入口中无已删除脚本的引用；源码包不再包含 scripts，
仍包含 uv.lock 和历史收据，wheel 仍包含包与 py.typed。
本次未修改业务源码，未重复执行没有测试用例的 pytest。


## T02：任务注册与输入校验

验证日期：2026-10-13。环境沿用 T01：Linux x86_64、CPython 3.12.13、uv 0.10.9、
Pydantic 2.14.1、pytest 9.1.1、pytest-asyncio 1.4.0、Ruff 0.17.0、mypy 1.20.2。

**状态：T02 已完成。** 实现与验收阶段止于 T02；T03—T10 未实现，当时未创建 Git Commit。

### 前置条件与实现边界

已读取项目 AGENTS.md、karpathy-guidelines skill、T02 prompt、README、架构、迭代计划及
T01 验收记录，检查当前源码与工作区。开始时工作区无未提交改动。
uv.lock 存在，locked 离线同步、原有 Ruff、mypy 和隔离构建均通过。
没有新增、升级依赖或修改全局环境；pyproject.toml、uv.lock 与 verification-t01.json 保持原样。
T01 历史文件指纹仅用于说明当时验收，不作为当前文件门禁。

本次实现 TaskRegistry、TaskDefinition、ExecutionPolicy、load_registry 及对应错误类型。
不访问数据库，不实现提交、领取、执行器、Worker、CLI、日志系统、退避调度或崩溃恢复。
默认策略为额外重试 0 次、退避基数 1s、上限 60s、超时 300s；范围及理由见 README。

### 行为证据

- tests/test_registry.py：注册后按稳定名称查找、原函数身份与直接 await；
  重名及非法名称、同步函数、async generator、参数形态、错误模型和注解被拒绝；
  失败不替换已有项，未知名称有明确错误，两份注册表互相隔离。
- tests/test_registry.py：合法与非法 Python/JSON 输入；模型的 extra=ignore/allow/forbid；
  严格类型、别名、datetime、Json 字段的 JSON 往返；不可序列化、必需字段被排除、
  非 JSON 对象的输出得到明确 InputSerializationError，不修改用户模型配置。
- tests/test_policy.py：非负严格整数、有限正时间、cap >= base、未知字段拒绝；
  默认值及合法边界、不可变且独立的任务策略副本，修改后续声明不影响已有任务定义。
  注册入口也重新校验绕过普通构造产生的策略对象。
- tests/test_loading.py：真实临时模块的 module:attribute 加载；格式错误、模块不存在、
  导入代码异常、属性缺失和属性不是注册表的失败路径及底层异常链。

首轮 77 项行为测试与 mypy 通过；Ruff 发现行宽、导入排序及测试中的未定义注解写法，
已修正。补充非 JSON 对象序列化测试后，最终 78 项全部通过。

### 最终检查结果

以下命令均设置 UV_OFFLINE=true、UV_CACHE_DIR="$PWD/.cache/uv"，使用已验收的本机缓存。
没有使用 --no-sync 替代 locked 检查，也没有新增占位测试。

| 检查 | 实际命令或方法 | 结果 |
| --- | --- | --- |
| 前置 locked 同步 | uv sync --locked | 退出码 0；解析 27 个包、审核 26 个安装包 |
| 完整行为测试 | uv run --locked pytest | 退出码 0；78 passed |
| Ruff | uv run --locked ruff check . | 退出码 0；All checks passed |
| 格式 | uv run --locked ruff format --check . | 退出码 0 |
| mypy strict | uv run --locked mypy | 退出码 0；4 个源码文件无问题 |
| 构建 | uv build | 退出码 0；生成 sdist，并从 sdist 构建 wheel |
| README 示例 | 从 README 提取 Python 示例到自动清理的临时 tasks.py，再用 uv run --locked python 执行 | 退出码 0；输出 count=3 |
| 显式加载示例 | 在临时模块路径调用 load_registry("tasks:registry") | 退出码 0；输出 demo.count |
| 示例类型检查 | uv run --locked mypy --strict 临时 tasks.py | 退出码 0 |
| 分发结构 | zipfile/tarfile 检查实际归档 | wheel 包含四个源码模块及 py.typed；sdist 包含锁文件、三个测试文件及 T03 prompt |
| wheel 接口 | 从 /tmp 以项目 Python 的 -I 模式优先加载 wheel，确认模块来源，再注册并做 JSON 往返 | 退出码 0；实际模块来自 dist wheel，使用项目已有依赖 |
| 改动检查 | git diff --check；核对依赖配置、锁文件及 T01 收据 | 退出码 0；三份前置文件未修改 |

README、架构文档的实现状态、迭代计划与本记录已同步，docs/prompts/T03.md 提供下一会话交接。
示例验证使用的临时目录已自动清理；原有 tests/.gitkeep 保留。

剩余限制：这里只保存执行策略，尚无持久化或执行能力；注册阶段不执行用户函数。
自定义模型需提供可重新校验的 JSON 表示，任意有损 serializer 不保证值完全相等。
更高 Python 版本仍未验证，MVP 仍需完成 T03—T10。

### T02 Git 提交

T02 验收后，用户追加要求创建 Git Commit。本次提交包含 T02 源码、行为测试、
说明文档、验收记录及 T03 交接 prompt；未继续实现 T03。

## T03：持久化提交与查询

验证日期：2026-10-13。环境沿用 T02：Linux x86_64、CPython 3.12.13、uv 0.10.9、
SQLAlchemy 2.0.54、aiosqlite 0.22.1、Pydantic 2.14.1、pytest 9.1.1、
pytest-asyncio 1.4.0、Ruff 0.17.0、mypy 1.20.2。

**状态：T03 已完成。** 实现与验收阶段止于持久化提交与查询，不继续 T04，当时没有创建 Git Commit。

### 前置条件与范围

已读取 AGENTS.md、karpathy-guidelines skill、T03 prompt、README、依赖配置、架构、迭代计划、
验收记录及现有源码和测试。开始时 Git 工作区干净；T02 最新提交为 9490166。
uv.lock 存在，使用已有完整离线缓存运行 uv sync --locked 成功。
当前 T02 的 78 项行为测试、Ruff、格式检查、mypy strict 和构建均通过，退出码均为 0。
T01 收据是历史证据，不使用其文件指纹作为当前门禁。

新增 api.py、config.py、db.py、storage.py，扩展公共快照、状态枚举与错误类型。
使用现有 SQLAlchemy 异步接口、aiosqlite 和本地 SQLite；没有新增或升级依赖。
pyproject.toml、uv.lock、docs/verification-t01.json 保留原样，原有 T02 源码行为及测试保持。

### 最终接口与行为证据

完整接口、参数、错误与可运行示例见 README 的“持久化提交与查询”。

- DatabaseConfig 只提供规范化的本地 database_path 和有上限的 busy_timeout_ms。
  SQLiteStore 支持显式 initialize、close 和 async context manager。
- jobs、attempts 以 schema 版本 1 建立，PRAGMA user_version 随 DDL 在同一写事务提交。
  重复初始化保留数据；拒绝不兼容版本、有表的未版本化数据库、缺表或列不匹配。
- Job/Attempt 为不可变快照，查询返回元组；不返回 ORM 或会话。
  时间以整数 UTC epoch 微秒保存，通过整数 timedelta 转为带时区 datetime。
- TaskForge.submit 先按稳定名称查找，再复用 T02 校验和 JSON 序列化；
  SQLiteStore.submit 接收 TaskDefinition。提交不执行任务函数或创建 Attempt。
  新 Job 为 UUID4、PENDING、次数 0，next_run_at=created_at，其余执行及错误字段为空。
  全部执行策略字段持久化；后续策略声明变更不影响已有 Job。
- get/list/history 查询不需要注册表。未知 Job（包括格式异常的字符串 ID）抛 UnknownJobError；
  已存在且未领取的 Job 历史为空。list 按 (created_at, id) 升序，以 JobCursor 为排他边界，
  limit 为 1—1000 严格整数；非法分页抛 PaginationError，空页为空元组。
  游标不依赖源记录存在，支持等效非 UTC 时区；不承诺跨页一致性快照。
- 独立短 AsyncSession；驱动不隐式 BEGIN，SQLAlchemy begin hook 统一发出普通读事务或
  BEGIN IMMEDIATE 写事务；每个连接启用外键并设置有限锁等待。
- tests/test_storage.py 的 74 项真实临时 SQLite 测试覆盖 schema、初始字段、JSON 往返、
  非法输入不入库、重开查询、策略快照、稳定分页、不可变历史、实际状态/次数/时间/文本长度
  约束、外键、索引、每个 Job 一个 RUNNING Attempt、并发初始化与 20 条并发提交。
- AFTER INSERT 的真实 RAISE(FAIL) 故障验证完整回滚；commit 前实际协程取消也回滚已 flush
  的记录。模拟 commit 应答丢失覆盖实际已提交及未提交两种结果，异常携带可查询的 job_id。
  commit 阶段取消转换为 SubmissionUncertainError，原 CancelledError 保留在异常链中。
- 真实读锁阻塞 COMMIT 的补充测试发现底层事务在 SQLAlchemy 认为事务已失效后仍保持开启，
  单靠关闭会话导致下一次 BEGIN 失败。已通过统一 _commit 在失败时 invalidate 连接修复；
  测试验证无残留记录、无嵌套 BEGIN，并可在同一存储再次成功查询与提交。
  该故障仍返回带 job_id 的 SubmissionUncertainError，调用方查询确认后决定是否重交。

### 最终检查结果

以下命令均设置 UV_OFFLINE=true、UV_CACHE_DIR="$PWD/.cache/uv"，使用现有已锁定环境。
没有以 --no-sync 替代 locked 检查，也没有添加占位测试。

| 检查 | 实际命令或方法 | 结果 |
| --- | --- | --- |
| 前置同步 | uv sync --locked | 退出码 0；解析 27 个包、审核 26 个安装包 |
| 全部行为测试 | uv run --locked pytest | 退出码 0；152 passed（T02 78 项、T03 74 项） |
| Ruff | uv run --locked ruff check . | 退出码 0；All checks passed |
| 格式 | uv run --locked ruff format --check . | 退出码 0；20 files already formatted |
| mypy strict | uv run --locked mypy | 退出码 0；8 个源码文件无问题 |
| 构建 | uv build | 退出码 0；生成 sdist，并从 sdist 构建 wheel |
| README 示例 | 提取实际两个 Python 代码块，在自动清理的临时目录通过 uv run --project 项目路径 --locked python 执行 | 两次退出码均为 0；count=3；Job 为 PENDING，jobs=1、attempts=0，下一页为空 |
| 示例类型检查 | uv run --project 项目路径 --locked mypy --strict 临时 tasks.py 临时 submit_query.py | 退出码 0；两个示例文件无问题 |
| 改动与前置文件 | git diff --check；git diff --exit-code -- pyproject.toml uv.lock docs/verification-t01.json | 退出码 0；依赖、锁文件与历史收据未修改 |

README、架构的实现状态、迭代计划与本记录已同步，docs/prompts/T04.md 提供下一独立会话交接。
README 示例的临时脚本及数据库自动清理；没有在工作区留下示例数据库或验收脚本。

剩余限制：T03 不领取或执行任务，无 Worker、重试调度、进程锁、恢复或 CLI；
不提供 schema 迁移、自动重交、业务去重及跨页一致性快照。错误文本上限由数据库约束执行，
后续执行层负责截断。父目录须已存在；更高 Python 版本尚未验证。MVP 仍需完成 T04—T10。

补充分发验证：更新验收文档后重新运行 uv build，退出码 0；用标准库 zipfile/tarfile 检查归档，
退出码 0。wheel 包含八个源码模块和 py.typed；sdist 包含 uv.lock、T03 存储测试、T04 prompt
及本验收记录。从项目外使用项目 Python 的 -I 模式优先加载实际 dist wheel，核对模块来源后
完成注册、提交、关闭重开与无注册表 get/list/history 查询，退出码 0。
该验证复用项目已安装依赖，没有安装新依赖；临时数据库与目录均自动清理。

### T03 Git 提交

T03 验收后，用户追加要求创建 Git Commit。本次提交包含 T03 源码、SQLite 行为测试、
接口说明、验收记录及 T04 交接 prompt；未继续实现 T04。


## T04：原子领取与完成

验证日期：2026-10-13。环境沿用 T03：Linux x86_64、CPython 3.12.13、uv 0.10.9，
使用已有完整离线缓存与锁定依赖，没有新增或升级依赖。

**状态：T04 已完成。** 本会话仅实现原子领取、完成及必要公共模型和错误；
验收阶段未继续 T05，没有创建 Git Commit。

### 前置条件与范围

已读取适用 AGENTS.md、karpathy-guidelines skill、T04 prompt、README、依赖配置、
架构、计划、验证记录、源码和现有测试。开始时工作区干净，T03 已提交为 938a49b。
uv.lock 存在；uv sync --locked 返回 0，解析 27 个包、审核 26 个安装包。
按当前文件复核 T01—T03：152 项已有行为测试、Ruff、格式检查、mypy strict 与构建全部返回 0。
T01 收据仅作历史证据；没有按历史指纹拒绝当前合法文件。

修改 storage.py、models.py、errors.py、__init__.py，增加 tests/test_attempts.py。
没有修改 schema 版本、表与约束、依赖配置、锁文件、历史 T01 收据或已有测试。
不创建后续业务模块，不执行用户任务函数。

### 接口与行为证据

完整接口、结果组合和可运行示例见 README 的“原子领取与完成”。

- claim_next() 返回 AttemptClaim 或 None；凭据含不可变 Job、job_id 属性和 number。
  BEGIN IMMEDIATE 中按 (next_run_at, created_at, id) 查找到期等待任务，条件 UPDATE
  再查状态、时间和预算；变为 RUNNING、次数加一，并插入对应 RUNNING Attempt。
  首次 started_at 保留；清空 next_run_at，参数与全部策略不变；commit 成功后才返回凭据。
- finish_attempt(job_id, number, outcome, *, status, next_run_at=None) 返回不可变 Job。
  校验 Job、当前尝试序号与双方 RUNNING 状态；未知 ID 抛 UnknownJobError，
  重复、过期或不匹配身份抛 AttemptConflictError。非法结果抛 InvalidAttemptResultError。
- AttemptOutcome 为不可变单次结果，支持 SUCCESS、FAILED 和 INTERRUPTED。
  Job 完成状态为 SUCCESS、FAILED 或调用方明确给出的 RETRY_WAIT；
  存储层只校验并保存，不计算重试决策或退避。CONFIG_ERROR 禁止重试，
  RETRY_WAIT 必须还有预算，next_run_at 带时区且严格晚于完成时间。
- 完成时间为事务中的 UTC 微秒时间：Attempt 总是结束，终态 Job 同时结束；
  重试等待的 Job.finished_at 为空。失败保存错误历史及当前摘要，成功清空当前摘要，
  历史错误不变。按字符将异常类型、摘要、traceback 截断至 256、2000、16000，
  保留调用方结果原值和现有数据库约束。
- 复用 db.py 的 begin hook 和 storage.py 的 _commit，未引入另一套事务开启方式。
  commit 前 SQL 故障完整回滚，取消传播并回滚；commit 开始后的故障或取消抛
  ClaimUncertainError / CompletionUncertainError，携带 job_id 与 number，
  保留异常链并使失败连接失效。调用方必须查询确认，不能直接执行不确定领取或重新执行任务。

tests/test_attempts.py 的 69 项真实临时 SQLite 文件测试覆盖：

- 屏障同时启动八个独立存储连接争抢同一 Job，仅一个成功；四个独立连接领取十二个 Job，
  根据写事务实际 UPDATE 顺序核对三个排序键，领取无重复。次数等于历史条数。
- 空队列、未到期、空到期时间、已到期但非等待状态、PENDING/RETRY_WAIT 预算耗尽均不领取；
  可替换内部微秒时钟，验证相等到期边界及未来一微秒不提前领取。
- 领取时间、次数、新 Attempt、策略和参数快照正确；重开后 RUNNING 与完成状态仍可查询；
  Job、Attempt、领取凭据不可变，旧快照不随后续完成变化。
- 成功、任务错误、超时、配置错误、中断，以及合法失败/中断的重试等待同时更新状态和历史；
  重领保持首次开始时间，成功清除摘要但保留第一次失败历史，Unicode 文本长度截断正确。
- 错误 Job、序号、未知 ID、过期、重复完成、Attempt 已非 RUNNING 与非法字段/结果组合均拒绝，
  保留原记录。屏障并发完成只有一次成功。
- AFTER INSERT attempts 的真实 RAISE(FAIL) 验证领取回滚已更新 Job 与已插入 Attempt；
  AFTER UPDATE jobs 的真实故障验证完成回滚已结束 Attempt 与已更新 Job。
- 事件阻塞 commit 时独立连接只能读到两个表的原状态，调用尚未返回；
  放行后状态和历史同时可见。commit 前取消已 flush 的领取或完成完整回滚。
- commit 应答丢失覆盖实际已提交和未提交两种状态及取消路径，错误凭据可查询；
  真实 SQLite 读锁阻塞 COMMIT 验证两项操作的连接失效、完整回滚及同一存储后续可用。
  BEGIN IMMEDIATE 锁故障有有限等待并返回 StorageError，未误报提交不确定。

首轮新增测试返回 1（65 passed、2 failed）：测试数据的远期整数超出 Python datetime 范围，
以及固定时间被误当成当前过去时间。已改为合法远期值、明确过去值，并用内部时钟验证精确边界。
首轮 Ruff 返回 1，提示 zip 缺少 strict 参数；已修正。后续新增测试及全部检查均通过。

### 最终检查结果

以下命令均设置 UV_OFFLINE=true、UV_CACHE_DIR="$PWD/.cache/uv"；
没有以 --no-sync 替代 locked 检查，没有占位测试。

| 检查 | 实际命令或方法 | 结果 |
| --- | --- | --- |
| 前置同步 | uv sync --locked | 退出码 0；解析 27 个包、审核 26 个安装包 |
| 全部行为测试 | uv run --locked pytest | 退出码 0；221 passed（T02 78、T03 74、T04 69） |
| Ruff | uv run --locked ruff check . | 退出码 0；All checks passed |
| 格式 | uv run --locked ruff format --check . | 退出码 0；21 files already formatted |
| mypy strict | uv run --locked mypy | 退出码 0；8 个源码文件无问题 |
| 构建 | uv build | 退出码 0；生成 sdist，并从 sdist 构建 wheel |
| README 三个示例 | 从实际代码块提取到自动清理临时目录，用 uv run --project 项目路径 --locked python 执行 | 三次退出码均为 0；count=3；PENDING/空历史；RUNNING SUCCESS 1 |
| README 示例类型 | uv run --project 项目路径 --locked mypy --strict 三个临时示例文件 | 退出码 0；3 个文件无问题 |
| 改动与前置文件 | git diff --check；git diff --exit-code -- pyproject.toml uv.lock docs/verification-t01.json | 退出码 0；依赖、锁文件与历史收据未修改 |

README、架构实现状态、迭代计划和本记录已同步，docs/prompts/T05.md 提供下一独立会话交接。
示例脚本与数据库随临时目录自动清理；未留下验收脚本或示例数据库。

剩余限制：T04 只保存领取与完成状态，没有 Executor、Worker、重试决策或调度、进程锁、
恢复、日志系统或 CLI；不提供自动重交与业务去重。提交结果不确定仍需查询确认；
更高 Python 版本尚未验证，MVP 仍需完成 T05—T10。


### T04 Git 提交

T04 验收后，用户追加要求创建 Git Commit。本次提交包含 T04 源码、真实 SQLite 行为测试、
接口说明、验收记录及 T05 交接 prompt；未继续实现 T05。


## T05：单次执行与超时

验证日期：2026-10-13。沿用既有 Linux x86_64、CPython 3.12.13、uv 0.10.9
与锁定依赖环境；全部命令使用 UV_OFFLINE=true、UV_CACHE_DIR="$PWD/.cache/uv"。

**状态：T05 已完成。** 仅实现单次执行、协作式超时与取消、执行上下文和 JSON 日志；
验收阶段没有继续 T06，没有创建 Git Commit。

### 前置条件与范围

已读取适用 AGENTS.md、karpathy-guidelines skill、T05 prompt、README、pyproject.toml、
架构、计划、验证记录、现有源码和行为测试。开始时工作区干净；
T04 最新提交为 f0d860f。uv.lock 存在，uv sync --locked 返回 0，
解析 27 个包、审核 26 个安装包，无新增或升级依赖。

按当前文件复核 T01—T04：221 项行为测试、Ruff、格式检查、mypy strict 及构建
全部返回 0。历史 T01 收据原样保留，不把旧文件指纹当作当前门禁。

新增 src/taskforge/executor.py、src/taskforge/logging.py 和 tests/test_executor.py；
扩展 errors.py 与 __init__.py 的实际公共接口。未改 models.py、schema、db.py 或
storage.py，没有 Worker、锁、策略层或 CLI 的空模块。
README、架构状态和迭代计划已同步，docs/prompts/T06.md 提供下一独立会话交接。

### 接口与行为证据

- Executor(registry).execute(claim) -> AttemptOutcome 接收已提交领取；
  再次按用户模型校验 Job.params_json，再调用 async 函数一次。未知名称和
  Pydantic ValidationError 返回 CONFIG_ERROR，不调用用户函数。
- 正常返回 None 得到无错误 SUCCESS；用户 Exception（包括 TimeoutError，
  以及由用户主动抛出的 ExecutorError）得到 TASK_ERROR，保留类型、摘要与包含
  原始 cause 的 traceback。错误详情生成故障属于框架错误。
- 超时仅由事件循环单调计时器触发，读取持久 Job.timeout_seconds，而非当前注册默认值。
  超时请求用户取消，实际退出后才返回 TIMEOUT；超时清理中的业务异常或正常返回
  不改变 TIMEOUT，非 None 返回仍为框架契约违规。
- 外部取消（包括超时清理期间的取消）等待真实退出后传播 CancelledError；
  用户自行取消同样传播。重复外部取消不再次打断用户 finally，不吞掉取消，
  不生成 INTERRUPTED outcome。执行未结束时不记录完成/中断事件。
- ExecutorError 保留原始异常链，EXECUTOR_ERROR 只用于框架日志与未来 Worker 级处理，
  不加入存储 ErrorKind、不转换成业务失败、不自动重试。
  非 None 返回以 TypeError 为 cause；validator TypeError、查找、错误格式化、
  计时器、任务创建、等待机制及日志故障均验证框架边界。
  框架等待故障也必须等待用户清理，诊断日志故障不覆盖原始异常或取消。
- 不可变 ExecutionContext 保存 job_id、attempt_number；
  contextvars 关联任务上下文，正常、失败、超时和取消均恢复原上下文。
  JSONFormatter 使用标准库 logging，字段为 time、level、event、job_id、
  attempt_number、error_kind；框架不安装 handler，不记录完整参数或异常内容。
  同步用户 handler 可复用上下文；跨线程队列 handler 的上下文捕获由调用方负责。
- tests/test_executor.py 的 40 项测试使用真实 async 函数和事件/屏障，
  验证模型重新校验、错误分类、异常链、返回契约、不可变结果、清理阶段的错误优先级、
  受控忽略取消与重复取消、并发日志隔离、嵌套上下文恢复和日志参数隐私。
  asyncio.sleep(0) 仅让已请求取消被事件循环处理，不以固定长 sleep 同步。
  3s 测试超时只作失效保护；所有受控运行协程都在 finally 中放行并清理。
- 真实 SQLite 测试关闭连接后，以不同默认超时的注册表执行已领取快照，
  禁止调用 submit/claim_next/finish_attempt，验证 Job 和 Attempt 仍为 RUNNING。
  另一项测试把真实业务异常结果传入 finish_attempt，确认 Unicode 摘要和
  traceback 按既有存储契约保存/截断，原始 outcome 不变。

首次源码 mypy 返回 1：直接检查声明为 None 的 await 返回值触发 func-returns-value；
已在运行时契约边界将 awaitable 扩宽为 Awaitable[object]，不改变用户函数声明。
首次 Ruff 返回 1：新增文件导入排序与行宽；已修正并格式化。
首轮 34 项新增行为测试通过，随后按明确边界补充至 40 项；没有占位测试。

### 最终检查结果

| 检查 | 实际命令或方法 | 结果 |
| --- | --- | --- |
| 前置同步 | uv sync --locked | 退出码 0；27 个包解析、26 个已安装包审核 |
| 全部行为测试 | uv run --locked pytest | 退出码 0；261 passed（T02 78、T03 74、T04 69、T05 40） |
| Ruff | uv run --locked ruff check . | 退出码 0；All checks passed |
| 格式 | uv run --locked ruff format --check . | 退出码 0；26 files already formatted |
| mypy strict | uv run --locked mypy | 退出码 0；10 个源码文件无问题 |
| 构建 | uv build | 退出码 0；生成 sdist，并从 sdist 构建 wheel |
| README 四个示例 | 从实际代码块提取，在自动清理临时目录以 uv run --project 项目路径 --locked python 执行 | 四次退出码均为 0；注册、查询、手动完成和执行完成均正确 |
| README 日志 | json.loads 解析 execute_once.py 实际 stderr，核对事件和身份 | 退出码 0；attempt_started / attempt_finished 同一 Job、尝试 1 |
| README 示例类型 | uv run --project 项目路径 --locked mypy --strict 四个临时示例文件 | 退出码 0；4 个文件无问题 |
| 分发结构与 wheel 闭环 | 标准库检查实际归档，并在项目外使用 Python -I 优先加载 wheel 完成提交、领取、执行和持久化完成 | 退出码 0；模块实际来自 wheel；包含新模块、py.typed、测试、T06 prompt 与验证记录 |
| 差异与前置文件 | git diff --check；git diff --exit-code -- pyproject.toml uv.lock docs/verification-t01.json | 退出码 0；依赖、锁文件和历史收据未修改 |

最终源码修正后完整测试、Ruff、格式和 mypy 已重跑通过；更新本记录后重新构建，
归档内容与隔离 wheel 闭环检查通过。示例脚本及数据库使用自动清理临时目录，
未留下临时验收脚本或示例数据库；保留正常 dist 构建产物与现有工具缓存。

剩余限制：取消依赖用户协作，忽略取消的协程将持续占用执行接口，
阻塞事件循环会阻塞计时器，没有硬超时或线程池。执行器要求调用方持有已提交领取，
不验证数据库所有权、不保存状态、不决定重试；外部取消后由未来 Worker/恢复流程处理尝试。
尚无 Worker、重试调度、进程锁、停机编排、恢复或 CLI；Python 高版本仍未验证，
MVP 仍需完成 T06—T10。


### T05 Git 提交

T05 验收后，用户追加要求创建 Git Commit。本次提交包含 T05 执行器、结构化日志、
异步行为测试、接口说明、验收记录及 T06 交接 prompt；未继续实现 T06。

## T06：Worker 与并发控制

验证日期：2026-10-13。环境：Linux x86_64、uv 0.10.9、CPython 3.12.13；
依赖保持 uv.lock 固定版本。

**状态：T06 已完成。** 仅实现 Worker 基础执行闭环与并发、进程锁；
未实现 T07 重试、T08 停机宽限期/恢复、CLI，未创建 Git Commit。

### 前置条件与环境证据

已读取 AGENTS.md、karpathy-guidelines、T06 prompt、README、pyproject.toml、架构、
计划、验证记录、现有源码和测试。开始时 T05 改动已暂存，按当前文件复核并保留；
最终工作区 HEAD 为 T05 提交 9a82f0a，本任务没有执行提交或修改历史。
uv sync --locked 退出码 0，27 个包解析、26 个已安装包审核，不新增或升级依赖。

默认沙箱中的原有测试停在首次 SQLite 操作，新增 Worker 测试也停在存储 fixture。
最小复现确认同步 sqlite3 可用、后台线程运行，但 asyncio.call_soon_threadsafe
不能唤醒等待的 Future；2s 存储初始化保护和 1s 跨线程通知保护均以 TimeoutError 退出。
自动权限审核允许沙箱外运行本地测试；不修改库、事务或测试断言规避环境问题。
两个挂起 pytest 进程经确认后以 SIGTERM 清理，对应命令退出码均为 143。
在允许跨线程唤醒的环境中，T01—T05 原有 261 项测试全部通过，退出码 0。
原有 Ruff、格式检查及 mypy 同样通过。

### 接口、文件与行为证据

新增 src/taskforge/worker.py、src/taskforge/locking.py、tests/test_worker.py 与
下一会话 docs/prompts/T07.md；扩展 config.py、errors.py、__init__.py 的实际接口。
未修改 storage.py、db.py、Executor、日志上下文或原有测试；依赖、uv.lock 和历史
T01 收据保持原样。README、架构实现状态与计划同步，历史记录保留。

- Worker(store, registry, config=None) 使用调用方已初始化且保持打开的存储。
  WorkerConfig 默认 concurrency=4、poll_interval_seconds=1.0，不可变，
  严格校验正整数与有限正数，拒绝布尔值、字符串及额外字段。
  Worker 实例只 run 一次；request_stop 是同步停止新领取接口。
- Worker 先获取规范化数据库路径加 .lock 的非阻塞独占 flock；整个生命周期保留描述符，
  退出关闭但不删除文件。独立 holder/contender 子进程证明失败方不能进入 claim_next，
  待领取 Job 与历史不变；另一个数据库可同时启动。正常退出后新进程可重新获锁，
  锁文件 inode 不变。路径含 .. 的另一拼写仍争用同一个规范化锁。
- 获锁后短查询拒绝未初始化版本，再复用 initialize 检查现有 schema；
  不隐式建立未初始化 schema。不兼容数据库报错并释放锁。
  固定异步槽位覆盖领取、执行和完成 commit。领取入口串行，锁等待者进入存储前检查停止。
  两个真实用户协程通过事件形成重叠，最大并发为 2，无槽位时其余 Job 未领取。
  用户悬挂期间独立 sqlite3 连接可执行 BEGIN IMMEDIATE 并写入，证明执行期间没有存储事务。
- 完成 commit 屏障证明用户已返回仍占槽，未提交状态对其他连接仍为 RUNNING，
  后续 Job 保持 PENDING；request_stop 后 run 等待写入，进程锁仍拒绝其他持有者。
  超时用户 finally 使用事件延迟实际退出，期间仍占槽持锁；结束后才保存 TIMEOUT。
- 成功、业务 ValueError、用户 TimeoutError、框架超时、未知名称、参数不匹配与后续成功
  均通过真实 Worker 持久化闭环验证。失败直接保存最终 FAILED，即使预算允许重试；
  配置错误不调用用户函数。每个已执行函数只调用一次，Attempt 数量、身份、时间、错误详情
  与 Job 状态对应，成功错误字段为空；单个失败不终止其他任务。
- 空队列两次真实领取之间有轮询延迟，不忙等；停止唤醒空槽位，无后续领取。
  停止发生在领取进行中时，该领取继续提交、执行、完成，等待领取者不再进入数据库。
  停止发生在用户运行或完成写入期间时，同样等待真实结束。
- SQLite trigger 注入领取和完成中间 SQL 故障，Worker 停止领取，原错误 cause 保留；
  领取故障完整回滚，完成故障保持 RUNNING，不重执行或伪造业务失败。
  另一项事件测试在用户运行时让后续领取抛 StorageError，第三个等待者不进入存储，
  已启动用户继续成功持久化后才传播错误。
- 8 项 commit 故障组合覆盖领取/完成、已提交/未提交、OSError/CancelledError。
  不确定领取不执行，不确定完成不重交/重执行；原 ClaimUncertainError 或
  CompletionUncertainError 带 job_id、number 和底层 cause 传播。
  重新打开存储查询状态与历史验证各实际结果，后续 Job 未领取。
- 非 None 返回触发真实 ExecutorError/TypeError cause，停止新领取，
  另一运行用户不被取消，等待它清理及成功保存后再传播故障；失败尝试保持 RUNNING。
  用户自行取消传播 CancelledError，不伪造 INTERRUPTED 完成。
- 外部重复取消 run 只停止新领取，运行用户和完成写入不被再次取消，
  结束前 run 不返回也不释放锁；全部完成后传播 CancelledError。
  遗留 RUNNING Job/Attempt 原样保留，不被领取，不在 T06 执行恢复。
- JSONFormatter 解析真实并发执行日志，逐 Job 检查 attempt_started / attempt_finished 身份；
  worker_error 对完成数据库故障、结果不确定和 ExecutorError 显式带 Job 与尝试，
  类别分别为 STORAGE_ERROR、EXECUTOR_ERROR；领取前故障无已知身份时为 null。
  类别仅用于日志，不扩展持久 ErrorKind，不输出完整参数或异常内容。
- 35 项新增测试（10 项配置边界、25 项运行行为）使用真实 SQLite、用户 async 函数、
  事件与必要子进程；定时等待仅用于真实轮询/框架超时，10s 测试截止只作失效保护。
  所有受控协程在 finally 放行并等待；所有子进程关闭输入、等待退出，失败时终止并回收。

首次新增 Ruff 检查退出码 1（导入排序及行宽），已修正并格式化；mypy 初次即通过。
Worker 首轮 35 项测试退出码 0；补强子进程提前退出时的清理后再次完整验收。

### 最终检查结果

以下命令均使用 UV_OFFLINE=true 和 UV_CACHE_DIR="$PWD/.cache/uv"，无依赖变更；
涉及 SQLite/子进程的运行使用自动审核允许的沙箱外环境。

| 检查 | 实际命令或方法 | 结果 |
| --- | --- | --- |
| 环境同步 | uv sync --locked | 退出码 0；27 个包解析、26 个安装包审核 |
| 原有测试复核 | uv run --locked pytest 指定原有六个测试文件 | 退出码 0；261 passed |
| 全部行为测试 | uv run --locked pytest | 退出码 0；296 passed（原有 261 + T06 35） |
| Ruff | uv run --locked ruff check . | 退出码 0；All checks passed |
| 格式 | uv run --locked ruff format --check . | 退出码 0；30 files already formatted |
| mypy strict | uv run --locked mypy | 退出码 0；12 个源码文件无问题 |
| 构建 | uv build | 退出码 0；生成 sdist，并从 sdist 构建 wheel |
| README 五个示例 | 从实际代码块提取，在自动清理临时目录以 uv run --project 项目路径 --locked python 执行 | 五次退出码均为 0；新增 Worker 示例保存 SUCCESS |
| 示例类型 | uv run --project 项目路径 --locked mypy --strict 五个临时示例文件 | 退出码 0；5 个文件无问题 |
| 归档与 wheel 闭环 | 检查 wheel/sdist；临时目录中 Python -I 优先加载 wheel 完成真实 Worker 闭环 | 退出码 0；实际导入自 wheel，Job/Attempt 成功，锁文件保留；包含新模块、测试和 T07 prompt |
| 差异与前置文件 | git diff --check；git diff --cached --check；git diff HEAD --exit-code -- pyproject.toml uv.lock docs/verification-t01.json | 退出码 0；依赖、锁文件和历史收据未修改 |

示例及隔离 wheel 使用自动清理临时目录，未遗留示例数据库、脚本或子进程；
保留常规 dist 构建产物和既有工具缓存。最终文档更新后重新构建，保证交接记录进入 sdist。

### 限制与下一会话

T06 仅形成单 Worker 的基础执行持久化闭环。普通失败即最终失败，没有重试决策与退避；
T07 按 Job 快照增加有限重试，下一独立会话使用 [T07 prompt](prompts/T07.md)。
T08 前遗留 RUNNING 尝试不自动恢复，外部取消不会记录中断，没有停机宽限期或主动取消编排。
用户超时清理若一直不结束，退出持续等待并持锁占槽；不提供硬超时、线程池或恰好执行一次保证。
CLI、多 Worker、租约、人工重放均未实现，Python 更高版本和 macOS 尚未验证。
完成 T06 后停止，不继续 T07，不创建 Git Commit。

### T06 Git 提交

T06 验收后，用户追加要求创建 Git Commit。本次提交包含 Worker 执行闭环、
有界并发、进程锁、行为测试、接口文档、验收记录与 T07 交接 prompt；未继续实现 T07。
README 与迭代计划同步提交状态，前述未创建提交的记录保留为验收阶段历史。

## T07：持久化重试

验证日期：2026-10-13。环境沿用 Linux x86_64、uv 0.10.9、CPython 3.12.13。

**状态：T07 已完成。** 仅实现纯重试决策、封顶指数退避与 Worker 持久化重试，
未实现 T08 停机宽限期/崩溃恢复或 CLI，未创建 Git Commit。

### 前置复核与环境

已读取 AGENTS.md、karpathy-guidelines、T07 prompt、README、pyproject.toml、架构、
迭代计划、历史验收记录及相关源码和测试。开始时工作区干净；
未修改依赖、uv.lock 或 docs/verification-t01.json，未调整历史文件指纹。
UV_OFFLINE=true、UV_CACHE_DIR="$PWD/.cache/uv" 下 uv sync --locked 退出码 0，
解析 27 个包、审核 26 个安装包，不新增或升级依赖。

默认沙箱再次在首项真实 SQLite 测试挂起，与 T06 环境限制一致；
已终止该次运行，退出码 130，不能视作通过。自动权限审核允许的沙箱外环境中，
修改前原有 296 项测试退出码 0；初步接入后原有 296 项再次退出码 0。
未修改 aiosqlite、db.py、事务开启 hook 或 _commit 清理语义。

### 接口与文件

- 新增 src/taskforge/policy.py：retry_delay_seconds(job) -> float，
  decide_completion(job, outcome) -> CompletionDecision；后者是 frozen dataclass，
  包含 status 与可空 retry_delay_seconds。纯函数只读取持久 Job 快照，不查注册表或数据库。
  第 k 次失败后延迟为 min(cap, base × 2^(k−1))；先比较浮点指数再计算，避免巨大幂和溢出。
- worker.py 在单次执行返回后调用纯决策，继续通过既有 finish_attempt 保存结果。
  TASK_ERROR/TIMEOUT 有预算则 RETRY_WAIT，无预算 FAILED；CONFIG_ERROR 直接 FAILED，
  SUCCESS 直接成功。完成 commit 后释放槽位，由原有轮询领取到期 Job。
  锁、停止、外部取消、数据库故障及 ExecutorError 的处理路径保持原契约。
- storage.py 的 finish_attempt 新增 retry_delay_seconds=None 关键字参数，
  原 next_run_at 绝对时间接口保留。相对延迟仅允许 RETRY_WAIT 的有限正数，
  拒绝布尔值、字符串以及同时提供绝对时间。存储在获得写事务并确认凭据后，
  从本次完成时间生成到期时间，避免数据库锁等待耗尽 Worker 预先计算的绝对间隔。
  延迟向上取整为微秒，最短 1 微秒；超出 datetime 范围封顶为 UTC datetime.max。
  预算、严格未来时间和原子完成校验保留；存储不决定重试或计算指数退避。
- 新增 tests/test_retry_policy.py（25 项纯策略测试）与 tests/test_retries.py
  （49 项真实 SQLite/Worker 测试）。原 tests/test_worker.py 的错误分类/终态测试
  改用 max_retries=0，保留原有单次执行断言；有预算失败转重试的行为由新增测试覆盖。
- 同步 README、docs/architecture.md、docs/implementation-plan.md，
  新增 docs/prompts/T08.md 作为下一独立会话交接；本次不实现恢复或主动取消编排。

### 行为证据

- max_retries=0、1、3，分别验证普通业务异常、用户 TimeoutError、框架 TIMEOUT，
  覆盖全部失败耗尽预算与最后一次成功。用户 TimeoutError 历史为 TASK_ERROR，
  框架超时清理结束后历史为 TIMEOUT；每个 Attempt 只调用一次。
  另验证有 5 次额外预算的成功、未知任务、参数不匹配均只生成一个 Attempt。
- 注册表替换成默认无重试、不同超时/退避策略后，已有 Job 仍使用提交时快照。
  逐次核对 ID、参数、策略、首次 started_at、Attempt 序号与历史，
  成功清空当前错误，先前失败 traceback 和摘要保留。逐条解析 JSON 日志，
  同一 Job 的各次 attempt_started/attempt_finished 身份和错误分类正确。
- 精确验证 0.25、0.5、1、2、3 的指数增长与封顶，等基数上限、精确幂边界、
  10**1000 的尝试序号、最小正 subnormal 到最大有限浮点数，结果始终正且有限。
  真实 SQLite 中验证小于 1 微秒延迟与巨大日期的持久表示；非法延迟保留 RUNNING。
- 独立 sqlite3 写者持有 BEGIN IMMEDIATE，观察实际完成事务进入锁等待，
  推进存储时钟 10 秒后释放锁；0.125 秒退避仍从取得完成事务后的时间开始。
  未为满足验证调整数据库事务机制。
- 单槽位先保存 RETRY_WAIT 后继续执行另一个任务；以真实空领取事件确认
  到期前 1 微秒不会执行，推进到精确边界才领取。真实时钟另验证两段 0.0625/0.125 秒
  退避，由正常轮询执行，历史 started_at 与函数调用时间均不早于持久 next_run_at。
- 关闭首个 Worker 存储后，两个独立新子进程分别在原 next_run_at 前 1 微秒和边界启动。
  前者不执行、保留原时间，后者成功创建第二个 Attempt，首次开始时间与失败详情仍保留；
  新进程注册默认无重试，不影响既有快照。
- 两个事件控制的失败任务及随后重试均可并发，最大用户并发为 2，上下文互相隔离。
  暂停真实 RETRY_WAIT 完成 commit 时，单槽位仍占用，后续两项未领取；
  重复取消 run 不打断写入、不提前释放锁，commit 完成后才传播 CancelledError。
- SQLite trigger 注入领取与 RETRY_WAIT 更新故障，前者完整回滚，后者 Job/Attempt
  保持 RUNNING；停止新领取且不重新执行，原 StorageError cause 保留。
  8 项 commit 故障组合覆盖领取/重试完成、已提交/未提交、OSError/CancelledError：
  保留不确定错误身份，重开存储确认状态，不执行不确定领取、不重交不确定完成。
- 第一次业务失败后，第二次返回非 None 触发 ExecutorError/TypeError cause；
  停止领取，第二次 Attempt 留 RUNNING，没有伪造业务失败或第三次执行。
  原有 T06 测试继续覆盖进程锁互斥、其他已运行槽位排空、停止、取消和遗留 RUNNING。

首轮新增 Ruff 检查退出码 1（4 处行宽），格式化后退出码 0。
新增测试首轮退出码 1（71 passed、1 failed）：槽位测试把提交时间误当首次领取时间，
二者因测试时钟推进相差 1 微秒；改为核对首次 Attempt 与 Job 的 started_at。
补充真实时钟和双槽位重试后，全部新增 74 项测试退出码 0。

### 最终检查结果

以下命令均使用 UV_OFFLINE=true、UV_CACHE_DIR="$PWD/.cache/uv"；
SQLite、子进程、示例和隔离 wheel 验证使用自动审核允许的沙箱外环境。

| 检查 | 实际命令或方法 | 结果 |
| --- | --- | --- |
| 环境同步 | uv sync --locked | 退出码 0；27 个包解析、26 个安装包审核 |
| 原有测试复核 | uv run --locked pytest，修改前及初步接入后 | 两次退出码均为 0；296 passed |
| 新增测试 | uv run --locked pytest tests/test_retries.py tests/test_retry_policy.py | 退出码 0；74 passed |
| 全部行为测试 | uv run --locked pytest | 退出码 0；370 passed，1 次线程告警，见下文 |
| 线程告警单项复核 | uv run --locked pytest tests/test_storage.py::test_real_commit_lock_failure_is_uncertain_and_rolls_back -W error::pytest.PytestUnhandledThreadExceptionWarning | 退出码 0；1 passed |
| 全量严格告警复核 | uv run --locked pytest -W error::pytest.PytestUnhandledThreadExceptionWarning | 退出码 0；370 passed，无告警 |
| Ruff | uv run --locked ruff check . | 退出码 0；All checks passed |
| 格式 | uv run --locked ruff format --check . | 退出码 0；34 files already formatted |
| mypy strict | uv run --locked mypy | 退出码 0；13 个源码文件无问题 |
| 构建 | uv build | 退出码 0；生成 sdist，并从 sdist 构建 wheel |
| README 五个示例 | 提取实际代码块，在自动清理临时目录以 uv run --project 项目路径 --locked python 执行 | 五次退出码均为 0；Worker 示例保存 SUCCESS |
| 示例类型 | uv run --project 项目路径 --locked mypy --strict 五个临时示例文件 | 退出码 0；5 个文件无问题 |
| 归档与 wheel 闭环 | 检查实际归档；项目外 Python -I 优先加载 wheel，运行真实时钟重试 | 退出码 0；FAILED → RETRY_WAIT → SUCCESS，两次 Attempt；原默认策略、首次时间与错误历史保留 |
| 差异与前置文件 | git diff --check；git diff --cached --check；git diff HEAD --exit-code -- pyproject.toml uv.lock docs/verification-t01.json | 退出码 0；依赖、锁文件与历史收据未修改 |

普通全量检查在原 test_real_commit_lock_failure_is_uncertain_and_rolls_back 附近报告一次
PytestUnhandledThreadExceptionWarning：aiosqlite 后台线程通知已关闭的事件循环。
上述单项和告警视作错误的全量复核均通过；记录该偶发观察，不宣称根因已经修复，
不修改驱动或已验证的存储清理语义。

测试协程均放行并等待，子进程等待退出、失败时终止回收；
示例与 wheel 数据库、脚本在 TemporaryDirectory 退出时清理，不遗留任务临时文件。
保留正常 dist 产物及既有工具缓存。文档记录补全后重新构建，确保验收记录进入 sdist。

### 限制与下一会话

T07 只调度有预算的业务失败和框架超时。RETRY_WAIT 重启保留原计划，
遗留 RUNNING 尚不恢复；外部取消仍等待已启动工作，无宽限期、主动取消或中断持久化。
协程不响应超时取消时继续占槽持锁，不提供硬超时、业务去重或恰好执行一次保证。
到期后执行可能受轮询间隔和槽位影响；微秒精度及 datetime 范围按上述规则表示。
CLI、多 Worker、租约、人工重放未实现，macOS 和更高 Python 版本尚未验证。
下一独立会话使用 [T08 prompt](prompts/T08.md) 实现停机与崩溃恢复；
完成 T07 后停止，不继续 T08，不创建 Git Commit。

### T07 Git 提交

T07 验收后，用户追加要求创建 Git Commit。本次提交包含纯策略决策、Worker 持久化重试、
完成事务相对延迟接口、74 项新增行为测试、接口文档、验收记录与 T08 交接 prompt；
未继续实现 T08。README 与迭代计划同步提交状态，前述未创建提交的记录保留为验收阶段历史。
提交前仅更新提交状态文档并重新构建，源码与已通过 370 项测试的版本一致；
git diff --check 和依赖、锁文件、历史 T01 收据检查通过。

## T08：停机与崩溃恢复

验收日期：2026-10-13；环境：Linux、Python 3.12.13。

**状态：T08 已完成。** 实现持锁启动恢复、宽限期停机及真实中断持久化。
未实现 CLI、多 Worker、租约、心跳、人工重放或业务去重；未创建 Git Commit，未继续 T09。

### 前置复核与环境

已读取项目约束、karpathy-guidelines、T08 prompt、README、pyproject.toml、架构、
计划、历史验收记录及相关源码与测试。开始时工作区干净；使用原 uv.lock 和完整缓存，
未新增或升级依赖，历史 verification-t01.json 原样保留。

uv sync --locked 退出码 0。默认沙箱 pytest 收集 370 项后挂在第一个真实 SQLite 测试；
确认该进程仍运行后清理，实际退出码 143。按 T08 prompt 使用自动权限审核允许的
沙箱外环境复核，修改前完整 370 项测试退出码 0。未修改 aiosqlite、db.py、_commit、
finish_attempt 的事务语义、Executor 或锁实现来适配环境。

### 接口与文件

- config.py：新增不可变 WorkerConfig.shutdown_grace_seconds，默认 30.0，
  严格接受有限非负数；0 立即请求协作取消，拒绝负数、布尔值、字符串、NaN 和无穷值。
- storage.py：新增 get_running_attempt() -> AttemptClaim | None，短查询读取首个遗留
  RUNNING 凭据，不授予所有权。Worker 必须先持锁；既有 finish_attempt 重新校验跨表身份
  并逐 Job 原子保存结果，复用 CompletionUncertainError，无 schema 变更或新提交机制。
- policy.py：INTERRUPTED 有预算时使用相同快照退避，否则 FAILED；业务失败和配置错误
  决策不变，领取已消耗预算，恢复不另增次数或 Attempt。
- worker.py：锁 → schema → 恢复 → 领取；执行器任务与数据库操作分开等待，使宽限期
  只取消执行。用户真正退出后才保存中断，重复取消不打断清理；已开始的领取、恢复和完成
  写入不被停机取消。恢复内部取消从 shield 的被取消任务取回原异常。
- tests/test_recovery.py：50 项真实行为测试；test_retry_policy.py 新增 3 项中断预算测试。
  test_worker.py 更新两个 T08 前的遗留状态断言，并加强第二进程不进入数据库的检查。
- README、架构与计划同步当前状态，新增 docs/prompts/T09.md 交接；历史记录保留。

### 行为证据

全部存储测试使用真实临时 SQLite 文件，用户函数为 async；事件/屏障同步关键阶段，
测试内部时钟仅控制数据库时间，实际宽限期由事件循环单调时钟验证。

- test_completion_within_grace_keeps_success：request_stop 与外部取消均停止新领取，
  宽限期内完成保留 SUCCESS，外部取消最后传播 CancelledError。
- test_grace_cancellation_waits_for_user_and_repeated_cancellation：组合正常/外部停机、
  协作/忽略取消、零/剩余预算；跨过实际宽限期后用户只收到一次取消请求，重复取消 run
  不打断清理。放行前 Job/Attempt 仍 RUNNING、锁及槽位保留，后续 Job 未领取；放行后
  INTERRUPTED 与 RETRY_WAIT/FAILED 原子保存，退避从清理结束后的完成事务时间开始。
- test_cooperative_stop_returns_only_after_interruption_commit 与
  test_parallel_shutdown_keeps_slots_and_json_identity_until_cleanup：清理结束但 commit
  未完成时仍不返回、不释放锁；两槽并发停机保留各自 JSON 身份，无额外领取。
  shutdown_incomplete 为 WARNING，含 job_id、attempt_number 和 INTERRUPTED，
  仅表示停机尚未完成，不伪造历史完成。
- test_grace_does_not_cancel_inflight_database_work：跨过宽限期及重复外部取消时，
  已进入领取/完成操作继续。晚确认的领取不开始用户函数而保存中断；已开始完成保留 SUCCESS。
- test_recovery_preserves_prior_history_waits_terminal_states_and_snapshots：保留先前失败
  历史、ID、参数、原策略和首次 started_at；当前第 2 次尝试使用封顶退避，不再增次数。
  未到期 RETRY_WAIT 的原时间及 SUCCESS/FAILED 均不变，第二次恢复不重复改写已恢复历史。
- test_recovery_sql_fault_rolls_back_and_prevents_claims：真实 SELECT 故障与 Attempt/Job
  更新触发器故障保留原异常链，跨表全部回滚，不进入正常领取。
- test_recovery_uncertain_commit_preserves_credentials_and_stops：真实事务未提交/已提交
  后分别注入 commit 故障或取消，保留 CompletionUncertainError 的凭据和 cause；
  独立连接可查准确状态，无自动重交或正常领取。
- test_recovery_cancellation_before_commit_rolls_back 与
  test_external_repeated_cancellation_during_recovery_drains_commit：commit 前内部取消
  保留原 CancelledError、回滚；外部反复取消等待已开始恢复 commit 完成并继续持锁。
- test_uncertain_commit_after_stop_never_reexecutes_or_resubmits：领取、成功完成、中断完成
  三阶段在停机后发生已提交/未提交、故障/取消的不确定结果，均保留原始异常和凭据，
  仅调用一次 commit，不执行不确定领取、不重交完成；独立读连接核对跨表状态。
- test_shutdown_fault_does_not_fabricate_completion_or_retry：停机中的真实 SQL 故障和
  ExecutorError 不伪装成业务失败或当场重试，未可靠保存的尝试留为 RUNNING。
- test_killed_worker_recovers_budget_and_demonstrates_duplicate_side_effects：真实 Worker
  子进程分别在执行中、文件副作用完成且成功 SQL 已 flush 而 commit 尚未开始时收到 SIGKILL，
  实际退出码 -9。下一 Worker 进程取得同一锁文件（inode 不变），按零/剩余预算恢复。
  有预算的 Job 在到期前重启仍等待，到期第 2 次执行重复写文件；最终 SUCCESS 保留
  原身份、首次开始时间和第 1 次 INTERRUPTED 历史，证明不保证恰好执行一次。
- test_recovery_killed_again_keeps_committed_jobs_and_rolls_back_pending_job：恢复第 1 个
  Job 已 commit、第 2 个跨表更新已 flush 尚未 commit 时强制退出。独立读连接始终看见
  第 1 个完整 RETRY_WAIT 与其余完整 RUNNING；下一进程保留第 1 个原时间/历史，仅恢复剩余项。
- 原进程互斥测试加强 contender 的 list/initialize/get_running_attempt 禁入断言；
  原注册、存储、Executor、错误分类、CONFIG_ERROR 不重试、并发与重试到期测试全部通过。

### 最终检查结果

以下命令均使用 UV_OFFLINE=true、UV_CACHE_DIR="$PWD/.cache/uv"；
SQLite、子进程及 README 示例使用自动审核允许的沙箱外环境。

| 检查 | 实际命令或方法 | 结果 |
| --- | --- | --- |
| 环境同步 | uv sync --locked | 退出码 0；27 个包解析、26 个安装包审核 |
| 修改前回归 | uv run --locked pytest | 退出码 0；370 passed |
| 针对性验证 | uv run --locked pytest tests/test_recovery.py tests/test_retry_policy.py tests/test_worker.py | 退出码 0；112 passed（最后加入的双槽停机测试在下行全量验证） |
| 全部行为测试 | uv run --locked pytest | 退出码 0；423 passed，无告警 |
| Ruff | uv run --locked ruff check . | 退出码 0；All checks passed |
| 格式 | uv run --locked ruff format --check . | 退出码 0；36 files already formatted |
| mypy strict | uv run --locked mypy | 退出码 0；13 个源码文件无问题 |
| 构建 | uv build | 退出码 0；生成 sdist，并从 sdist 构建 wheel |
| README 五个示例 | 提取实际 Python 代码块，以 uv run --project 项目路径 --locked python 执行 | 五次退出码均为 0；Worker 保存 SUCCESS |
| 示例类型 | uv run --project 项目路径 --locked mypy --strict 五个示例文件 | 退出码 0；5 个文件无问题 |
| 差异及保护文件 | git diff --check；git diff --cached --check；git diff HEAD --exit-code -- pyproject.toml uv.lock docs/verification-t01.json src/taskforge/db.py src/taskforge/executor.py src/taskforge/locking.py | 退出码 0；依赖、锁文件、历史收据和原有事务/执行/进程锁实现未修改 |

针对性测试初期发现冻结时钟下 Job 按 UUID 排序造成测试身份假设不成立，以及 shield
合成取消异常导致恢复丢失原取消对象；已分别修正测试时序及 Worker 取消传播，并在全量验证。
未缩减真实 SQLite、强制退出、原子状态或协作取消验证。

测试均在 finally 放行并等待协程，子进程被终止后 communicate/wait 回收；
本次保留的 pytest 临时数据库目录及挂起基线测试目录已清理，其他会话临时目录保留；
README 示例使用 TemporaryDirectory 自动清理。沙箱外进程检查未发现遗留 pytest/uv。
保留正常 dist 产物与工具缓存；本记录补全后重新构建，确保交接文档进入 sdist。

### 限制与下一会话

取消与超时依赖用户协作；用户忽略取消时一直等待、占槽持锁，不提供硬超时。
不确定结果需先查询；框架错误无法可靠完成的尝试待下一次持锁启动恢复。
恢复完成时间是确认时间，实际崩溃时刻未知；成功 commit 前的外部副作用可能重复。
首期仅单机单 Worker；macOS、更高 Python 版本、CLI 尚未验收。
下一独立会话使用 [T09 prompt](prompts/T09.md) 实现完整 CLI；本次停止，不创建 Git Commit。

### T08 Git 提交

T08 验收后，用户追加要求创建 Git Commit。本次提交包含持锁启动恢复、宽限期协作停机、
中断预算决策、53 项新增测试、接口文档、验收记录与 T09 交接 prompt；未继续实现 T09。
README 与迭代计划同步提交状态，前述未创建提交的记录保留为验收阶段历史。
提交前仅更新提交状态文档并重新构建，源码与已通过 423 项测试的版本一致；
git diff --check 和依赖、锁文件、历史 T01 收据检查通过。

## T09：完整 CLI

验证日期：2026-10-13。环境：Linux x86_64、CPython 3.12.13、uv 0.10.9、
pytest 9.1.1、Ruff 0.17.0。使用既有依赖与完整安装环境，没有新增或升级依赖。
**状态：T09 已完成。** 本会话只实现 T09，不创建 Git Commit，不继续 T10。

### 接口与改动

- 新增 `src/taskforge/cli.py`：Typer 的 init/submit/worker/list/show/history 六个命令。
  `pyproject.toml` 声明安装入口 `taskforge = taskforge.cli:main`，支持模块入口。
- init 显式初始化或校验；submit 显式加载可信注册表，读取 UTF-8 文件或 stdin JSON，
  调用 TaskForge.submit，成功只输出已提交 Job ID。输入错误不入库、不执行任务。
- list/show/history 只使用 TaskForge 查询，不导入任务模块。完整快照保留策略与 UTC 时间，
  list 输出排他分页游标，history 区分未知 Job 与已有 Job 的空历史。
- worker 管理存储与 Worker 生命周期；SIGINT/SIGTERM 仅调用 request_stop，
  重复信号不取消用户清理；等待真正退出及持久化后才关闭存储。
  使用既有 JSONFormatter 向 stderr 输出框架日志，不配置 root logger。
- CLI 错误以 JSON 输出 stderr；输入 2、未知名称/ID 3、注册加载 4、schema 5、锁 6、
  存储 7、框架 8、不确定结果 9、协程取消 130。正常命令与已完成停机返回 0；
  业务/配置失败通过 Job 与 Attempt 查询，不直接使 Worker 退出。
- 同步 README 的参数、输出、退出码、信号语义与限制；更新架构实现状态、迭代计划，
  新增 `docs/prompts/T10.md`。核心 API、存储、事务、Worker、Executor、锁实现原样保留。

### 真实行为证据

`tests/test_cli.py` 新增 53 项验证，均使用真实命令子进程与 SQLite 文件：

- 安装入口及所有 --help 正常返回，不加载故障任务模块或创建数据库；init 可重复校验，
  submit 输出规范 UUID4，合法文件/stdin 输入按模型规范化保存，策略与 Python 接口一致。
- 非法 JSON、NaN、模型输入、UTF-8、文件/数据库路径、分页、Worker 配置和 Typer 语法
  均有实际退出码断言；未知名称/ID、注册加载、schema 与存储失败明确报告。
  未初始化的查询/Worker 不创建表或改变 user_version。
- 查询时任务模块即使抛导入异常仍正常；分页排他且有序、最后空页游标为 null，
  已有 Job 空历史与未知 ID 明确区分，历史按 number 升序。
- 两个 worker 命令的真实进程锁互斥。SIGINT 与 SIGTERM 均覆盖宽限期内正常成功、
  宽限期后取消、等待清理、忽略取消与重复信号；清理前 Job/Attempt 仍 RUNNING，
  第二进程仍退出 6，排队 Job 不被领取。放行后才持久化 INTERRUPTED 并退出 0，
  stderr 的 shutdown_incomplete 携带正确 Job/Attempt 身份；锁文件保留。
- 业务失败按预算产生三次 TASK_ERROR 后 FAILED；缺失任务与模型版本不匹配只产生
  一次 CONFIG_ERROR，不调用函数。返回契约违规退出 8，尝试保持 RUNNING 待恢复；
  用户自行取消退出 130，先保存 INTERRUPTED。
- 在真实提交后注入确认丢失，分别得到 SubmissionUncertainError、ClaimUncertainError、
  CompletionUncertainError 与退出码 9；报告凭据可用 show/history 核对。
  不确定领取未调用函数，不确定完成只调用一次。恢复阶段同样输出原凭据，不重新执行。
- 将真实完成 SQL 替换为访问不存在表的 SQL，得到 StorageError 与退出码 7，
  完成事务回滚，Job/Attempt 保持 RUNNING，函数只执行一次；没有伪装业务失败。
- 重启遵循持久重试到期时间；CLI Worker 被 SIGKILL 后下一进程将原 Attempt 原子中断，
  退避恰从确认完成时间开始，后续实际调用时间不早于到期，历史为 INTERRUPTED/SUCCESS。
  恢复时间用单个 SQLite 查询捕获，避免 CLI 启动耗时跨过短暂 RETRY_WAIT 的观察竞争。
- 构建 wheel，在项目外创建独立虚拟环境，安装实际 wheel，确认包来自该环境的
  site-packages；全部帮助及 init/submit/worker/list/show/history 闭环通过。
  删除任务模块后仍可查询。uv pip check 返回 0，实际入口以 SIGTERM 完成停机。

隔离安装最初使用 uv export 的带 hash requirements；离线缓存中缺少可解析的当前平台
greenlet wheel，uv pip install 返回 1。最终验证复制项目已安装的固定版本第三方依赖，
排除 TaskForge、所有 .pth 与 editable 路径，再以 uv pip install --no-deps 安装真实 wheel。
此证据证明分发产物与入口在项目外可运行，不声称全新联网依赖安装已验证。

初次路径错误测试发现：不存在父目录的驱动连接失败可能在解释器退出阶段异常终止。
CLI 现在连接前拒绝不存在的父目录与非文件路径，返回明确存储错误；真实 SQL 故障仍单独
验证。未修改 aiosqlite、db.py begin hook、_commit 或原有事务清理语义。
初次故障注入的签名与执行层选择错误已修正，最终全量验证覆盖修正后的测试。

### 最终检查结果

以下命令均使用 UV_OFFLINE=true、UV_CACHE_DIR="$PWD/.cache/uv"；真实 SQLite 与 CLI
子进程在自动权限审核允许的沙箱外运行。默认沙箱下修改前测试在 12 秒观察上限内挂起，
timeout 退出 124；改用沙箱外后原有 423 项全部通过，没有用窄测试替代回归。

| 检查 | 实际命令或方法 | 结果 |
| --- | --- | --- |
| 环境同步 | uv sync --locked | 退出码 0；27 个包解析，26 个安装包审核 |
| 修改前回归 | uv run --locked pytest -q | 退出码 0；423 passed |
| 全部行为测试 | uv run --locked pytest | 退出码 0；476 passed，185.60 秒，无告警 |
| Ruff | uv run --locked ruff check . | 退出码 0；All checks passed |
| 格式 | uv run --locked ruff format --check . | 退出码 0；39 files already formatted |
| mypy strict | uv run --locked mypy | 退出码 0；14 个源码文件无问题 |
| 构建 | uv build | 退出码 0；生成 sdist，并从 sdist 构建 wheel |
| 项目外安装入口与闭环 | test_wheel_installed_outside_project_complete_command_cycle | 全量测试通过；真实 wheel 安装、包来源检查、全部命令及 pip check 通过 |
| 保护文件与差异 | git diff --check；uv.lock、历史 T01 收据及既有核心文件差异检查 | 退出码 0；只添加 CLI 与入口、测试和对应文档 |

全部子进程以 communicate/wait 回收，测试 finally 放行清理后等待；本次最终测试使用
独立临时根，验收后删除该目录及本会话残留测试目录，保留其他会话目录与正常工具缓存。
最终文档更新后重新构建并检查归档，确保 README、验证记录与 T10 prompt 进入 sdist。

### 限制与交接

首期仍为单机单 Worker，不提供用户取消命令、人工重放、迁移框架或业务去重。
超时与停机依赖用户协作，忽略取消时继续持锁等待；框架日志报告未完成，不承诺硬超时。
外部副作用完成但 SUCCESS commit 前崩溃可能重复执行；T08 的真实证据与历史记录保留。
本次验证为 Linux/Python 3.12，macOS、更高 Python 版本及全新联网依赖安装未验收。
下一独立会话使用 [T10 prompt](prompts/T10.md) 完成场景示例、幂等验证和完整 MVP 文档；
本次停止，不继续实现 T10，不创建 Git Commit。

### T09 Git 提交

T09 验收后，用户追加要求创建 Git Commit。提交包含六个 CLI 命令、打包入口、53 项
新增测试、接口文档、验收记录和 T10 交接 prompt；未继续实现 T10。
提交前仅同步提交状态文档并重新构建，源码与通过 476 项测试的版本一致；
git diff --check、uv.lock、历史 T01 收据与既有核心文件的保护检查通过。
前述不创建提交的记录保留为验收阶段历史。

## T10：场景验证与文档

验证日期：2026-10-13。环境：Linux x86_64、CPython 3.12.13、uv 0.10.9、
pytest 9.1.1、Ruff 0.17.0；依赖版本见 [真实场景收据](verification-t10.json)。
使用已有完整缓存与固定依赖，没有新增或升级依赖。
**状态：T10 已完成当前环境的场景、质量检查与文档验收，结果见下表。**
本会话仅处理 T10，完成后停止，不创建 Git Commit。

### 实现与可复现入口

- `examples/local_service.py` 是仅监听 loopback 的 asyncio TCP API 替身，提供固定 JSON
  记录、下载字节及按输出目标发生一次的上游故障；无第三方凭据或新依赖。
- `examples/local_tasks.py` 提供同步、下载备份和 scratch 维护任务，以及可控超时、
  协作停机和独立业务数据库的幂等对照。业务逻辑不进入核心包。
- `examples/python_cycle.py` 用真实 Python 接口提交、执行、关闭存储、无注册表重开查询
  及分页；同时验证 JSON 内容、备份字节和维护文件，不只检查 Job 状态。
- `examples/verify_scenarios.py` 在临时目录中运行 Python 和实际安装的 CLI，保存真实
  快照、退出码、日志、调用和副作用；`tests/test_scenarios.py` 从项目外调用该脚本。
- README 增加一条命令的场景入口，`docs/scenarios.md` 提供独立 Python/CLI 步骤与
  幂等边界。架构与计划同步实现状态；sdist 包含 examples，wheel 保持核心库与 CLI。
  `pyproject.toml` 只增加 sdist 的 examples 目录；源码、uv.lock、历史 T01 收据未修改。

在项目根目录执行以下命令可重新生成当前实测收据（路径由调用方选择）：

```bash
uv run --locked python examples/verify_scenarios.py --output /tmp/taskforge-t10.json
```

本次保留的 `verification-t10.json` 来自真实运行，脚本退出 0，时间为
`2026-10-13T15:08:10.935163+00:00`。它包含 44 次退出 0 的 CLI 操作、1 次非法输入退出 2，
各正常停机 Worker 的退出码 0，以及两个 SIGKILL 子进程的返回值 -9。
本地服务作为验收 fixture 最后被 SIGTERM 回收，返回值 -15；它不是 Worker。
收据中的源码 SHA256 已与当前文件逐个核对；临时路径统一替换为 `<temporary>`。

### 真实业务、停机与崩溃证据

Python 与 CLI 的同步任务均先得到真实服务返回的临时故障，历史为 TASK_ERROR/SUCCESS；
重试领取时间不早于前次完成加 0.1 秒。下载文件的实际字节为
`TaskForge local backup fixture\n`；维护删除 expired.tmp 并保留 keep.txt。
输出父目录不存在时，两次 TASK_ERROR 后最终 FAILED。可控等待任务两次 TIMEOUT 后
FAILED，第二次用户 finally 的清理标记已写入；业务失败和超时不使 Worker 停机非零退出。

SIGINT 场景先通过用户进入标记和 CLI 快照确认 RUNNING，再请求宽限期为 0 的协作停机。
用户 finally 清理结束、Attempt INTERRUPTED 与 Job FAILED 提交后 Worker 退出 0。
排队 Job 保持 PENDING、空历史，后续独立 Worker 才执行；正常完成场景使用 SIGTERM。
既有回归另外验证重复信号、忽略取消继续持锁、清理及数据库写入期间不提前释放槽位。

两组崩溃均使用真实子进程、核心 SQLite 和独立业务 SQLite。只在该验收子进程中暂停
SUCCESS SQL 已 flush、`_commit` 尚未调用原实现的时刻；未改核心文件或伪造持久状态。
父进程读取屏障和 CLI 快照，确认业务副作用已提交、Job/Attempt 已提交状态仍 RUNNING，
随后 SIGKILL。再次查询与崩溃前快照相等，证明 SUCCESS 未提交且被回滚。
下一真实 CLI Worker 取得原锁、恢复中断、按持久退避再次领取；历史为 INTERRUPTED/SUCCESS。

| 业务对照 | 稳定 Job ID | 崩溃返回值 | 实际调用 number | 已提交副作用 | value 合计 |
| --- | --- | --- | --- | --- | --- |
| 普通 INSERT | `57cd9a10-3435-4fec-bc48-9c5b14043a7c` | -9 | 1、2 | 2 条 | 14 |
| Job ID 唯一键 + INSERT OR IGNORE | `66e08aec-8a0f-4783-ba00-2cb95020c4da` | -9 | 1、2 | 1 条 | 7 |

calls 表证明幂等组第二次函数确实运行；effects 唯一 Job ID 与副作用在同一业务事务中
提交，重复执行只保留第一次效果。这个唯一键不去重再次 submit 生成的新 Job ID，
也不让外部 API 和核心 SUCCESS 原子提交。框架仍可能重复执行；业务幂等须由任务实现。

### T01—T10 当前证据复核

以下证据来自当前工作区的完整回归和实际产物，不仅复用历史计数或旧文件指纹。

| 要求 | 当前证据 |
| --- | --- |
| T01 工程、锁文件、类型与构建 | locked sync、Ruff、mypy strict、sdist/wheel 检查；test_wheel_installed_outside_project_complete_command_cycle 实际隔离安装 |
| T02 注册、输入、独立策略、可信加载 | test_registry.py、test_policy.py、test_loading.py 覆盖拒绝重名/同步函数、模型与 JSON 往返、注册隔离、显式导入 |
| T03 显式初始化、持久提交与查询 | test_storage.py 覆盖重开、策略快照、非法输入不入库、schema 拒绝、空历史/未知 ID、排他分页、SQL 回滚和不确定提交 |
| T04 原子领取与完成 | test_attempts.py 用独立连接验证并发唯一与顺序、状态和历史同事务、完成身份、重复完成拒绝、SQL 故障回滚、commit 不确定性 |
| T05 单次执行、超时与上下文 | test_executor.py 验证 TASK_ERROR/TIMEOUT/CONFIG_ERROR、返回契约与框架异常链、协作取消、并发 contextvars/JSON 日志 |
| T06 锁、轮询、槽位与故障停领 | test_worker.py 的 overlap、slot_and_lock、real_process_exclusion、real_sql_failure 等验证有界重叠、用户执行不占事务、持锁及故障不重执行 |
| T07 持久重试 | test_retry_policy.py、test_retries.py 及 CLI restart 测试验证 N+1 预算、退避封顶、完成事务时间、快照、释放槽位、重启遵循到期与完整历史 |
| T08 停机与恢复 | test_recovery.py 覆盖真实 SIGKILL、预算耗尽、恢复再次崩溃、重复取消/忽略取消、数据库操作不被停机取消；T10 另证实业务幂等 |
| T09 六命令、输出与退出码 | test_cli.py 验证全部帮助、参数/JSON/文件/配置错误、0/2/3/4/5/6/7/8/9/130、查询不导入任务、信号与恢复、不确定结果凭据及项目外 wheel 完整闭环 |
| T10 三用途、完整闭环与幂等 | test_scenarios.py、verification-t10.json、README 实际五段代码及独立 Python 场景入口；业务内容、真实子进程退出与持久记录均已检查 |

项目外 wheel 验证沿用 T09 的固定依赖复用方式：在临时独立环境中复制已安装的第三方
依赖，排除 TaskForge、所有 .pth 和 editable 路径，再以 `uv pip install --no-deps`
安装新构建 wheel；包来源为该环境 site-packages，`uv pip check` 与全部帮助、六命令
闭环通过，删除任务模块后仍可查询。此验收不等同于全新联网安装依赖。

### 最终检查结果

命令使用 `UV_OFFLINE=true`、`UV_CACHE_DIR="$PWD/.cache/uv"`。真实 SQLite、loopback
服务、CLI 与子进程在自动权限审核允许的沙箱外运行。默认沙箱的单项真实 SQLite 检查
在 12 秒内挂起，timeout 返回 124；原驱动、db.py begin hook 和核心事务清理保持原样。

| 检查 | 实际命令或方法 | 结果 |
| --- | --- | --- |
| 环境同步 | uv sync --locked | 退出码 0；27 个包解析，26 个安装包审核 |
| 修改前回归 | uv run --locked pytest --basetemp=/tmp/taskforge-t10-baseline | 退出码 0；476 passed，163.02 秒 |
| 新场景测试 | uv run --locked pytest tests/test_scenarios.py --basetemp=/tmp/taskforge-t10-scenarios -q | 退出码 0；1 passed，27.48 秒 |
| 全部行为测试 | uv run --locked pytest；PYTEST_ADDOPTS 指定本次临时根 | 退出码 0；477 passed，181.95 秒，无告警；包含项目外 wheel 安装、帮助与全部命令闭环 |
| 实测收据 | uv run --locked python examples/verify_scenarios.py --output docs/verification-t10.json | 退出码 0；真实三用途、失败/重试、超时、信号、崩溃与幂等均通过 |
| Ruff | uv run --locked ruff check . | 退出码 0；All checks passed |
| 格式 | uv run --locked ruff format --check . | 退出码 0；45 files already formatted |
| mypy strict | uv run --locked mypy | 退出码 0；14 个源码文件无问题 |
| README 五段代码 | 提取实际 Python 代码块，在临时目录以 uv run --project 项目路径 --locked python 执行 | 五次退出码 0；注册 count=3、提交 PENDING、原子完成 SUCCESS、Executor SUCCESS、Worker SUCCESS |
| README 示例类型 | uv run --project 项目路径 --locked mypy --strict 五个示例 | 退出码 0；5 个文件无问题 |
| 独立 Python 场景入口 | uv run --project 项目路径 --locked python examples/python_cycle.py --port 实际端口 | 退出码 0；三个 SUCCESS，临时数据清理 |
| 构建 | uv build | 退出码 0；生成 sdist，并从 sdist 构建 wheel；最终记录更新后重建归档 |
| 产物及保护检查 | zipfile/tarfile 比较归档与当前文件；git diff --check；uv.lock、verification-t01.json 与 src/taskforge 差异检查 | 退出码 0；sdist 含完整 examples 与实测记录，wheel 含 py.typed、CLI 入口和当前核心；保护文件未修改 |

场景脚本所有子进程在 finally 中 communicate/wait 回收，Python Worker 在 finally 请求
停止并等待，存储与本地连接显式关闭。原始收据证明场景临时目录已移除。
验收后删除本次 baseline、场景、全量测试临时根、默认沙箱挂起的单项测试目录以及
README 检查脚本与临时输出；保留其他会话的临时目录、正常工具缓存和 dist 分发产物。
沙箱外进程检查未发现本次遗留 Worker、服务、场景脚本或 pytest。

### 验证边界

当前证据覆盖 Linux x86_64/CPython 3.12 的 T01—T10 行为；macOS、更高 Python 版本、
全新联网依赖安装仍未验收，因此不宣称所有目标平台的 MVP 验收完成。
真实第三方 HTTP API、凭据与大型备份的性能不属于本地替身的验证结果。
所有架构范围保持：单机单 Worker、协作超时与停机；未扩展 Cron、租约、心跳、
用户取消、人工重放、Web UI、迁移框架或核心业务去重。

### T10 Git 提交

T10 验收后，用户追加要求创建 Git Commit。提交包含本地三用途示例、Python/CLI 场景
验收脚本、真实崩溃与业务幂等收据、新增端到端测试、sdist 示例分发和同步文档。
提交前仅更新提交状态文档并重新构建，源码与通过 477 项测试的版本一致；
git diff --check、收据源码指纹及 uv.lock、历史 T01 收据、既有核心源码的保护检查通过。
前述不创建提交的记录保留为验收阶段历史。

## G01：配置、HTTP 与业务状态基础

验证日期：2026-10-14。**状态：G01 已验收；G02—G05 待实现、待验收，整体真实 GitHub 接入待验收。**
环境为 WSL2/Linux x86_64、项目 CPython 3.12.13、uv 0.10.9。未升级 Python；验收阶段未创建 Git Commit。
G02 实际接口与采集范围已交接到 [prompts/G02.md](prompts/G02.md)，本会话停止于 G01。

### 前置复核、外部契约与授权

开始时 `git status --short` 显示用户已有 `docs/architecture.md`、`docs/implementation-plan.md`
修改，以及未跟踪的 business-integration-plan.md、github-integration-plan.md、
obsidian-sync-requirements.md、rss-requirements.md、G01.md 和 obsidian-sync-plan.md。
保留这些来源文件；本次仅在允许范围增加实现和同步对应文档，未改 Obsidian/RSS 需求。

读取 AGENTS.md、karpathy-guidelines、架构、实施与接入计划、验证记录、T01/T10 收据、
README、依赖配置、核心公开接口、local_tasks 示例及相关行为测试。
当前公开接口为 TaskRegistry.task/get、TaskDefinition.validate_input/validate_json/serialize_input、
TaskForge.submit/get/list/history、SQLiteStore、Worker.run/request_stop 和 get_execution_context。
业务使用独立 aiosqlite 数据库，不访问核心私有表。当前核心与示例的 SHA-256 匹配 T10 收据，
T01/T10 两份历史收据逐字节匹配 HEAD；历史计数没有替代本次完整回归。

本次重新检查 `/usr/bin/gh`：`gh auth token --hostname github.com` 退出码 0，有非空输出；
仅记录退出码与是否存在，未输出 token。GH_TOKEN、GITHUB_TOKEN 当时均未设置。
这只证明本机读取成功，不证明远端权限或七来源兼容性。

本次联网复核以下官方契约，未进行七来源真实采集：

- [GitHub API versions](https://docs.github.com/en/rest/about-the-rest-api/api-versions)：
  2026-03-10 仍在支持列表，固定 X-GitHub-Api-Version 保持。
- [GitHub REST best practices](https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api)：
  串行请求、ETag/304、Link、Retry-After、主限流 reset 及无其他期限的二级限流至少等待一分钟。
- [HTTPX async](https://www.python-httpx.org/async/)、
  [timeouts](https://www.python-httpx.org/advanced/timeouts/)、
  [environment variables](https://www.python-httpx.org/environment_variables/)：
  AsyncClient 显式关闭，分项超时外另加总时限；生产 trust_env=True，受控测试 False。
- [Commits/Compare](https://docs.github.com/en/rest/commits/commits#compare-two-commits)、
  [Git trees](https://docs.github.com/en/rest/git/trees#get-a-tree)、
  [Releases](https://docs.github.com/en/rest/releases/releases#list-releases)、
  [Issues](https://docs.github.com/en/rest/issues/issues#list-repository-issues)：
  列表 per_page 上限 100、Compare 文件上限 300、tree 截断需逐子树、Issue 响应可能包含 PR。
  这些业务完整性检查由 G02/G03 实现，G01 不提供采集结果。

先生成 `github = ["httpx>=0.28.1,<1"]` 和 uv.lock 差异，再展示具体版本并询问安装。
新增 httpx 0.28.1、httpcore 1.0.9、anyio 4.15.1、certifi 2026.7.22、h11 0.16.0、idna 3.20；
用 tomllib 比较 HEAD 与当前锁文件，已有 27 个锁定包的版本全部保持。
用户回答“批准安装”后才执行 `uv sync --locked --extra github`，退出码 0，安装 6 个新依赖并重建
editable TaskForge；没有全量升级。离线 `uv lock --offline --no-python-downloads` 首次退出码 1，
因缓存无 HTTPX；默认沙箱联网 uv lock 退出码 2，代理 TCP 连接被拒绝。
获自动审批的沙箱外 `uv lock --no-python-downloads` 退出码 0，只解析锁文件，未安装环境。

### 当前实现与行为证据

新增 `integrations/github/` 的 config、models、auth、http、storage、locking、lifecycle、errors，
以及必要包初始化文件和 default-sources.json；未添加采集器、任务注册或业务命令占位模块。
默认目录 `.taskforge/github/`，独立 `business.sqlite`，PRAGMA user_version=1。
所有操作使用新连接，写事务为短 BEGIN IMMEDIATE，默认锁等待 5000ms，可配置有限非负整数。
来源代次历史与检查点分开保存；预算调整保留基线，仓库/类型/扫描/筛选变更须显式 rebaseline。

业务 schema 保存 sources/generations、intents、batches、attempts、entities/versions/changes、
skill_snapshots/skill_changes、release_ids、http_cache、cooldown。
实体身份为远端仓库 ID + kind + 对象 ID（Commit 用 SHA）；内容版本另加规范化指纹。
技能变化唯一键为仓库 ID、前后 SHA、路径；批次唯一键 refresh_id，并核对完整非敏感输入。
重复内容版本去重，变化记录按批次保留；Issue updated_at 规范化 UTC，旧快照拒绝覆盖。
BatchWrite 的完整性、业务记录、变化、清单、检查点及完成标记同事务提交。
apply 在 commit 回执或提交后清理故障时等待操作结束，关闭原连接后独立检查完成标记：
已完成返回确认结果；明确未完成抛 StateError；核对失败抛 CommitUncertainError，不直接重做。

HTTP 固定根地址、Accept 和 API 版本。AttemptBudget 每次尝试共用请求数与累计冷却等待；
请求总时限包括门控、冷却、缓存访问及重定向，分项超时保持计划默认值。
门控采用共享异步互斥及 api.lock，覆盖请求、响应关闭与缓存/冷却持久化。
每页 per_page=100，默认 10 页；缓存键完整包含 GET、URL/查询、Accept、版本，排除凭据。
304 仅复用对应缓存的 body/Link，第一页 304 仍请求后页。
外部主机、异常分页和过滤条件变化、预算中断均明确失败，不返回伪完整结果。
主限流与 Retry-After 期限取较晚值，二级限流无其他期限时使用持久次数指数等待，
已有冷却取 max；新进程与换 token 的新客户端都先核对同一数据库冷却。

| G01 要求 | 当前证据 |
| --- | --- |
| 七来源、严格配置、预算边界、JSON 往返 | test_github_config.py：73 项；全部字段非法类型、非有限值、负值/零、Commit 上限、未知字段、重复 ID、类型不相容、默认策略、来源与路径规范化 |
| schema、重开、唯一身份、代次与事务 | test_github_storage.py：17 项；真实文件、版本 0/2/999 拒绝、重开、批次/实体/版本/技能去重、预算保持、旧代次拒绝、真实 SQLite trigger 故障回滚、独立读者的提交前/后原子可见性 |
| commit 不确定与取消清理 | storage 测试用事件暂停真实 commit，分别验证已提交/未提交后独立核对、核对失败不重做、rollback/close 故障后仍核对；重复取消期间连接实际结束前来源锁仍持有 |
| 完整分页、缓存与预算 | test_github_http.py：34 项；多页、查询/路径键隔离、304 原 body/Link、第一页不变后页更新、无缓存 304、异常 Link/跳页/端点/外部主机、页/请求预算、失败保留检查点 |
| 超时、限流和敏感信息 | HTTP 测试覆盖分项异常、总等待超时后等待 transport 清理、主/二级限流、秒数/HTTP-date Retry-After、期限合并、指数等待、累计等待、普通 403；假 token 不进入异常 cause/context、日志、缓存键、数据库，拒绝转义后的敏感响应 |
| 认证与子进程回收 | test_github_auth.py：5 项；GH_TOKEN/GITHUB_TOKEN/真实 gh 替身顺序，缺失诊断，stderr 不泄漏，spawn 与 communicate 期间取消及重复取消后实际子进程已退出、回收 |
| 真正跨进程互斥与恢复 | test_github_processes.py：6 项；真实来源 flock 冲突/不同来源可并行、同来源多锁实例异步串行、不同来源进程共享 API 门控、锁 inode 保留、种子进程保存冷却并退出后新 token 进程实际请求数为 0 |
| HTTP 与客户端关闭的锁生命周期 | process 测试以事件暂停 transport 清理及 AsyncClient.aclose；重复取消仍持来源锁与适用 API 锁，实际清理/关闭后才释放，全部任务与子进程 finally 等待结束 |

### 实际检查命令与退出码

默认沙箱执行 `timeout 12s .venv/bin/python -m pytest tests/test_storage.py -x -q
--basetemp=/tmp/taskforge-g01-probe` 退出码 124，再次复现历史 aiosqlite 跨线程通知挂起。
获自动审批的沙箱外单项核心提交测试退出码 0，1 passed；后续真实资源测试均采用该方式，
未改核心驱动、事务或线程通知语义。

依赖同步、锁文件解析在沙箱外联网；随后检查使用 `UV_OFFLINE=true`、
`UV_CACHE_DIR="$PWD/.cache/uv"`，同步时 `UV_PYTHON_DOWNLOADS=never`。
全量 pytest 另设置 `PYTEST_ADDOPTS=--basetemp=/tmp/taskforge-g01-full`；
pyproject.toml 的 pytest `pythonpath=["."]` 导入源码业务包，进程测试显式设置项目根 PYTHONPATH。
mypy 实际检查核心及 integrations，保持 strict 与 warn_unreachable，未跳过业务模块。

| 检查 | 实际命令/方法 | 结果 |
| --- | --- | --- |
| 授权后环境同步 | uv sync --locked --extra github | 退出码 0；33 个锁定包解析，6 个新依赖安装，已有版本保持 |
| 配置及存储首轮 | uv run --locked --extra github pytest tests/test_github_config.py tests/test_github_storage.py -q --basetemp=/tmp/taskforge-g01-storage | 退出码 0；85 passed |
| HTTP 首轮 | uv run --locked --extra github pytest tests/test_github_http.py -q --basetemp=/tmp/taskforge-g01-http | 退出码 0；29 passed |
| 认证及进程首轮 | uv run --locked --extra github pytest tests/test_github_auth.py tests/test_github_processes.py -q --basetemp=/tmp/taskforge-g01-processes | 退出码 0；10 passed |
| 存储/HTTP 加固复验 | uv run --locked --extra github pytest tests/test_github_config.py tests/test_github_storage.py tests/test_github_http.py tests/test_github_auth.py tests/test_github_processes.py -q --basetemp=/tmp/taskforge-g01-foundations | 退出码 0；134 passed；之后补入客户端关闭测试，在全量中验证 |
| 全量行为回归 | uv run --locked --extra github pytest | 退出码 0；612 passed，123.01 秒，无告警；477 既有 + 135 新增 |
| Ruff | uv run --locked --extra github ruff check . | 退出码 0；All checks passed |
| 格式 | uv run --locked --extra github ruff format --check . | 退出码 0；首轮 66、G02 交接文档完成后 67 files already formatted |
| mypy strict | uv run --locked --extra github mypy --strict src/taskforge integrations | 退出码 0；24 个源码文件通过 |
| 构建 | uv build | 退出码 0；生成 sdist，并从 sdist 构建 wheel；文档交接完成后再次构建 |
| 归档边界及内容 | 标准库 tarfile/zipfile 对实际归档与当前文件逐字节核对 | 退出码 0；sdist 含 G01 业务/默认配置/测试/文档及 G02 prompt；wheel 含核心、py.typed、CLI 入口，不含 integrations |
| 保护与差异 | 历史指纹/HEAD 逐字节核对；git diff --check | 退出码 0；核心、示例、T01/T10 收据未改，已有锁定版本保持，无空白错误 |

### 限制与清理

所有新 HTTP 测试使用 MockTransport、假凭据、trust_env=False，认证行为测试使用临时可执行文件
或真实 Python 子进程替身；默认测试不依赖个人凭据或 GitHub 网络。
没有建立真实七来源基线，没有远端凭据有效性、采集内容、真实 Worker 故障或 direct 对照结论。
G02 项目/技能、G03 Issue、G04 注册/CLI/提交核对/导出、G05 真实接入均保持待实现/待验收。

凭据只存内存；诊断调用方须继续提供非敏感业务原因，不能把远端异常正文或 token 交给
record_attempt。来源上下文须包住 HTTP 客户端和全部数据库/凭据清理；G01 已验证该嵌套生命周期，
后续编排须复用。取消是协作式等待实际结束，不承诺清理硬时限。
生产遵循本机代理和证书环境；其他平台、全新环境的全套依赖安装仍不在本次证据范围。

本次创建的 probe、storage、HTTP、process、foundations、full 测试临时根及 G02 草稿已清理；
真实子进程、客户端、连接、协程均由 finally 等待结束，最终进程检查未发现本次遗留进程。
保留正常 .cache/uv 工具缓存和 dist 分发产物，不删除其他会话的文件。


### G01 Git 提交

G01 验收后，用户追加要求创建 Git Commit。提交包含配置、认证、HTTP、独立业务存储、
资源清理及锁基础，135 项新增行为测试，github 可选依赖及锁文件，接入计划、验收记录
和 G02 交接 prompt。此前“不创建提交”的要求与记录保留为验收阶段历史。
Obsidian/RSS 的独立需求及 Obsidian 交接文档保留在工作区，不纳入本次提交。

提交准备仅同步文档状态，源码与通过 612 项测试的验收版本一致；核心与历史收据保持。
文档更新后重新构建，核对 sdist/wheel 内容与 git diff --check；不继续实现 G02。

## G02：项目与技能采集

验证日期：2026-10-14。**状态：G02 已验收；G03—G05 待实现、待验收，整体真实 GitHub 接入待验收。**
环境沿用 G01：WSL2/Linux、CPython 3.12.13、uv 0.10.9、httpx 0.28.1。
完成范围仅为项目与技能采集及 [G03 交接](prompts/G03.md)；未创建 Git Commit。

### 前置证据与外部契约

读取项目 AGENTS.md、karpathy-guidelines、writing-for-agents、G02 prompt、架构、实施与接入计划、
验证记录、T01/T10 历史收据、README、依赖声明与锁文件、业务源码/默认配置/测试、
核心公开接口及 local_tasks 示例。开始时仅有三个无关未跟踪文件：
obsidian-sync-requirements.md、rss-requirements.md、prompts/obsidian-sync-plan.md，均保留原样。

当前核心与示例的 18 个 SHA-256 匹配 T10 历史收据；pyproject.toml、uv.lock、T01/T10 收据
逐字节匹配 HEAD。历史 T01 依赖指纹仅作为历史，不覆盖 G01 的 github extra 声明。
本次修改前完整回归为 612 passed，119.31 秒，退出码 0；分页、缓存、预算、代次、业务事务、
跨进程互斥、取消清理与持久冷却均由当前实际测试复核，没有以历史计数代替前置证据。

重新读取 API 版本 2026-03-10 的官方文档，没有读取个人凭据或执行真实七来源采集：

- [Commit 与 Compare](https://docs.github.com/en/rest/commits/commits?apiVersion=2026-03-10)：
  Commit 列表可固定 sha；Compare 必须分页，文件列表最多 300 个，不能替代完整技能树。
- [Release](https://docs.github.com/en/rest/releases/releases?apiVersion=2026-03-10)：
  完整列表用于核对已发布身份；draft 可因权限可见，须主动排除；普通 tag 不在 Release 范围。
- [Git tree](https://docs.github.com/en/rest/git/trees?apiVersion=2026-03-10)：
  truncated=true 必须非递归逐子树读取；完整性无法证明时不能推断删除。

### 实现与实际接口

新增 project.py、skills.py、remote.py 及 tests/test_github_project.py。
业务 schema 仍为 v1；核心源码、公开接口、驱动、事务、依赖版本和历史收据原样保留。
HTTP 新增 iter_pages，paginate 仍只返回完整合并列表，原 G01 行为复验通过。
BusinessStore 新增 completed_input(data)，在网络前核对批次完成标记与完整输入。

refresh_project(data, credential, ExecutionIdentity(execution_id, attempt_number), *,
transport=None, trust_env=True) -> bool 是独立于 TaskForge 包装的入口。
调用方先 initialize/register_source；来源锁覆盖完成核对、最新代次/检查点读取、
HTTP 客户端、网络、诊断及 apply。已完成相同输入返回 False，不发网络、不重复 apply；
冲突输入拒绝。rebaseline 指向入口已经创建的新代次，采集重试不重置代次。
collect_project 仅构造完整 BatchWrite，要求调用方持来源锁；后续 direct 可复用同一入口。

项目检查点保存 repository_id、规范化 repository、default_branch、sha。
每次读取默认分支后固定 commit 与其 tree SHA；末尾再次核对配置路径和规范路径的仓库 ID。
Compare 逐页核对 status/ahead_by/behind_by/total_commits/base/merge-base，核对页间一致性、
完整数量、SHA 唯一性与目标身份。初始 Commit 最多 20 条，全标 initial，覆盖明确有限历史。
分叉、回退、404/422 不可比较或增量超预算抛 RebaselineRequired，保留旧检查点。

Release 初始登记全部可见已发布 ID，只保存按 UTC published_at、数值 ID 降序最近最多 10 条内容。
后续保存新观察 ID，持续核对已存内容，初始窗口外旧 ID 不监测编辑；draft 排除，prerelease 包含。
内容含 id、tag_name、name、body、created_at、published_at、prerelease、html_url，时间规范化 UTC，
指纹忽略无关 API 扩展和 updated_at。已存 ID 消失保留实体/版本/身份，覆盖保存 missing_saved_ids
及诊断。“首次观察到”单独统计其中发布时间位于观察时刻前 7 天的数量，发现旧发布不计为近期发布。

技能仅扫描固定 commit 原生树中的普通 SKILL.md（100644/100755）；symlink/submodule 不展开，
覆盖记录排除数量与定义变化边界。recursive 截断结果整份丢弃，以完整 root/subtree 重建；
任一子树截断、身份不符、响应异常或预算中断均失败。重复路径、类型不符、异常祖先亦拒绝。
完整快照差分产生 added/modified/deleted；重命名为删除加新增，变化唯一身份沿用仓库 ID、
前后 SHA、路径，固定提交的 before_url/after_url 保存在覆盖记录。同 SHA 快照变化明确拒绝。

三部分共同通过才一次 apply。覆盖还含前检查点、执行身份、观察 UTC、请求数与固定目标；
网络在业务事务外，每次尝试只有一个 AttemptBudget。缓存、冷却及失败诊断可单独持久化，
不代表业务批次完成。remote.parse 的载荷校验错误不保留远端 ValidationError 异常链。

### 行为证据

新增 87 项行为测试全部使用 MockTransport、假凭据、trust_env=False 与真实临时业务 SQLite。
失败测试逐表核对 sources/generations、batches、entities/versions/changes、技能快照/变化及
Release 身份，证明确实未发生部分业务提交，而非只检查单个 SHA。

| G02 要求 | 当前实际证据（tests/test_github_project.py） |
| --- | --- |
| 有限 Commit 基线、前进、无变化 | finite_baseline_forward_and_unchanged；invalid_commit_baseline；small_baseline：20 条/短窗口、全部 initial、完整增量、同 SHA 不调用 Compare、非法基线无提交 |
| Compare 分页与完整数量 | compare_complete_pagination_and_target_fixed；compare_rejects_unproven_history；compare_later_page_incomplete：3 页、重复/缺失 SHA、目标身份、计数与页间元数据不一致、后页故障和页预算 |
| 分叉、回退、不可比较与上限 | compare_rejects_unproven_history；old_sha_not_comparable；increment_commit_limit_boundary：status、base/merge-base、404/422、500 成功/501 失败及更小配置上限，超限第一页即诊断 |
| 重定向及远端身份 | repository_redirect_keeps_remote_identity；remote_id_replacement：同 ID 更名继续，开始/采集中/旧路径 ID 换仓库均拒绝 |
| Release 身份与初始窗口 | release_all_ids_initial_window；release_full_pagination：105 个 ID 完整分页、只存 10 条、UTC 时间及 ID 排序、draft 排除/prerelease 包含、空列表和可空字段 |
| 旧 Release 编辑与发现统计 | release_all_ids_initial_window；release_fingerprint_tracks_each_content_field；release_fingerprint_normalizes_time：已存旧内容继续编辑、窗口外旧 ID 不监测、全部字段指纹、等价时区不误报、首次观察旧/近期发布区分 |
| 消失及分页失败 | release_disappearance；release_uncertain_coverage：实体/版本/身份保留并诊断，后页失败、重复 ID、缺字段/非法时间不提交 Commit |
| 完整技能清单与截断补齐 | skill_complete_native_tree；truncated_recursive_tree：普通可执行文件、symlink/submodule 边界、共享子树在不同前缀完整展开、partial phantom 丢弃 |
| 子树失败与错误删除防止 | failed_subtree；invalid_recursive_tree：子树 HTTP/截断/身份/预算、异常类型/路径/循环；逐表保留旧记录和快照；只有 empty_complete_tree 能证明删除 |
| 稳定变化及固定链接 | skill_complete_native_tree_changes_rename_and_fixed_links；same_commit_snapshot：新增、修改、删除、重命名；重复目标保留同一变化身份，链接固定前后 SHA |
| 幂等、代次与重试 | complete_replay；generations_rebaseline；generation_changed_during_network；failed_old_job_retry；failed_initial_attempt_retry：完成输入冲突、旧代次拒绝与已完成确认、旧历史保留、新基线、旧 Job 重试从最新成功 SHA 开始 |
| 固定目标、共用预算与原子回滚 | branch_moving_mid_attempt；all_collectors_share_one_actual_request_budget；sql_failure_rolls_back：分支途中前进仍读取既定树/历史、请求总预算、真实 checkpoint trigger 故障整批回滚 |
| 锁及取消清理 | same_source_refreshes_serialize_checkpoint_read；project_cancellation_keeps_source_lock：事件屏障证明第二刷新只在第一提交后读取检查点；重复取消直到 transport 清理结束才释放来源锁 |

全量同时复验 G01 的真实进程锁、持久冷却、缓存分页、事务回滚/不确定提交与客户端关闭，
以及现有核心 SQLite、CLI、Worker、停机与崩溃测试；G02 没有启动新的真实 Worker 故障实验。

### 实际命令、结果与分发核验

首次直接 uv sync --locked --extra github 退出码 2，因为默认用户缓存目录只读。
改用既有工作区缓存后成功；没有新增/升级依赖，也没有用 --no-sync 绕过锁文件。
下表 uv 命令均设置 UV_OFFLINE=true、UV_CACHE_DIR="$PWD/.cache/uv"。
真实 SQLite、CLI 和子进程测试通过自动审批允许的沙箱外环境执行；未改驱动或降低验证范围。

| 检查 | 实际命令或方法 | 结果 |
| --- | --- | --- |
| 同步 | uv sync --locked --extra github | 退出码 0；33 个包解析、32 个安装包审核 |
| 修改前全量 | uv run --locked --extra github pytest -q | 退出码 0；612 passed，119.31 秒 |
| 采集及 G01 分页/存储首轮 | uv run --locked --extra github pytest tests/test_github_project.py tests/test_github_http.py tests/test_github_storage.py -q --basetemp=/tmp/taskforge-g02-targeted | 退出码 0；123 passed，14.08 秒 |
| 扩展后的 G02 行为 | uv run --locked --extra github pytest tests/test_github_project.py -q --basetemp=/tmp/taskforge-g02-project | 退出码 0；87 passed，14.19 秒；随后微调共享预算中断位置，最终全量复验 |
| 最终全量 | uv run --locked --extra github pytest -q --basetemp=/tmp/taskforge-g02-full | 退出码 0；699 passed，133.35 秒，无告警；612 既有 + 87 新增 |
| Ruff | uv run --locked --extra github ruff check . | 退出码 0；All checks passed |
| 格式 | uv run --locked --extra github ruff format --check . | 退出码 0；71 files already formatted |
| 核心及业务 strict | uv run --locked --extra github mypy --strict src/taskforge integrations | 退出码 0；27 个源码文件通过 |
| 构建 | uv build | 退出码 0；sdist 及从 sdist 构建的 wheel；交接文档落盘后再次构建 |
| 实际分发内容 | uv run --locked --extra github python，标准库 tarfile/zipfile 核对实际归档与当前文件 | 退出码 0；首次 21 个业务/配置/测试/G02 文件逐字节匹配；最终含 G03 与更新文档，wheel 仅核心、py.typed、CLI 和包元数据 |
| 保护文件与差异 | 核心/示例 T10 指纹；git diff HEAD --exit-code -- src/taskforge examples pyproject.toml uv.lock docs/verification-t01.json docs/verification-t10.json；git diff --check | 退出码 0；保护文件不变，无空白错误 |

实现初期 Ruff 行长/导入与 mypy 的 JsonValue 容器、Literal、元组类型诊断已修复；
最终检查保持 strict，未跳过业务模块或删减测试。pytest 的 pythonpath=["."] 与已有进程
显式 PYTHONPATH 设置沿用 G01；没有改变核心或测试配置。

### 限制、清理与交接

REST 分页不提供跨请求冻结快照；固定 Commit/tree SHA 与完整身份分页不声称还原所有中间事件。
初始化只有 20 条 Commit、10 条 Release 内容；技能只记录定义文件变化，不下载或执行技能。
已发布 Release 的近期计数采用观察时刻前 7 天窗口；丢失已关注 ID 只诊断，不删除历史。
其他平台、真实远端权限与七来源兼容性仍未验收。

G03 Issue、G04 注册/业务 CLI/提交关联与 reconcile/导出/Worker 故障、G05 真实接入和对照仍待完成。
采集本身不调用核心提交，不提供正式收据文件或导出；业务数据库中的批次覆盖是当前结果依据。
[架构](architecture.md)、[实施计划](implementation-plan.md)、[接入细则](github-integration-plan.md)
及 README 已同步，G03 prompt 列明实际接口、覆盖边界、检查证据与 Issue 范围。

本次测试的协程与真实资源由 finally 等待清理；任务创建的 pytest 临时根与交接草稿已删除，
保留正常 dist 产物和工具缓存。无关用户文件未改，未实现 G03，也未创建 Git Commit。

### G02 Git 提交

G02 验收后，用户追加要求创建 Git Commit。提交包含项目/技能采集、必要分页与完成输入核对、
87 项新增行为测试、接口说明、验收记录和 G03 交接 prompt；此前未创建提交的记录保留为验收阶段历史。
Obsidian/RSS 的三个独立文档保留在工作区，不纳入本次提交；未继续实现 G03。

提交准备仅同步文档提交状态，实现与通过 699 项测试的版本一致；核心、依赖与历史收据保留。
文档更新后重新构建并核对实际归档内容，git diff --check 与暂存差异检查通过。
