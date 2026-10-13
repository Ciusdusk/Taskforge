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
