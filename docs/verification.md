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
