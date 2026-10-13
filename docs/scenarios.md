# 本地场景与业务幂等

这些示例对应 API 数据同步、异步文件下载备份和后台维护三个用途，使用固定的小数据和
loopback TCP 服务，无需网络服务、第三方凭据或额外依赖。按 README 同步环境后，均从
项目根目录运行。`local_service.py` 使用一行 JSON 请求/响应协议模拟上游 API，
不是第三方 HTTP 接口的兼容实现；替换成真实客户端时保留输入模型和任务边界即可。

## 一条命令完成验收

```bash
uv run --locked python examples/verify_scenarios.py --output /tmp/taskforge-t10.json
```

该命令直接断言业务输出和真实持久状态，失败非零退出。它自动分配端口、建立临时目录、
管理进程并清理文件；只留下显式指定的收据。验收时不要用 Python 的 `-O` 关闭断言。
默认沙箱可能阻塞真实 aiosqlite 的跨线程通知，本项目验收使用获准的沙箱外环境，
没有替换驱动或改变事务实现。

| 场景 | 执行与可检查结果 |
| --- | --- |
| Python 完整闭环 | `python_cycle.cycle` 初始化、提交三个 Job、运行 Worker，再关连接、无注册表重开查询和分页；全部 SUCCESS |
| CLI 完整闭环 | 实际安装入口调用 init/submit/worker/list/show/history；非法 port 退出 2 且不入库，合法提交输出 UUID |
| 数据同步 | 上游按 target 第一次响应临时失败；0.1 秒持久退避后成功，历史为 TASK_ERROR/SUCCESS；本地 JSON 含两个固定记录 |
| 下载备份 | 异步请求本地服务，解码并保存固定字节；验证文件内容，而非只查询 SUCCESS |
| 后台维护 | 只删除演示 scratch 目录的 `*.tmp` 普通文件；`keep.txt` 保留 |
| 持续业务失败 | 不存在的输出父目录导致两次 TASK_ERROR，最终 FAILED；正常停机 Worker 退出 0 |
| 超时 | 两次 0.15 秒框架超时，均记录 TIMEOUT；用户 `finally` 清理完成后才结束尝试；最终 FAILED |
| 协作停机 | 任务进入后发 SIGINT，宽限为 0；清理标记写入、Attempt INTERRUPTED 后 Worker 才退出 0；排队 Job 尚未领取；其他正常 Worker 使用 SIGTERM |
| 崩溃恢复与幂等 | 两个真实 Worker 在业务 commit 后、核心 SUCCESS commit 前收到 SIGKILL（子进程返回 -9）；重启后同一 Job 的历史为 INTERRUPTED/SUCCESS |

## 单独运行 Python 示例

终端 A 启动本地服务；它打印操作系统分配的端口：

```bash
uv run --locked python examples/local_service.py
```

终端 B 将 `TASKFORGE_DEMO_PORT` 设置为打印出的数字，然后运行：

```bash
TASKFORGE_DEMO_PORT=12345
uv run --locked python examples/python_cycle.py --port "$TASKFORGE_DEMO_PORT"
```

输出三个成功 Job、完整历史、同步记录、备份内容和维护后剩余文件。
Python 示例结束后清理自己创建的临时目录；服务结束使用终端 A 的 Ctrl-C。

## 单独运行 CLI 示例

保持终端 A 的服务运行，在终端 B 的项目根目录执行以下步骤。
`TASKFORGE_DEMO_PORT` 必须为服务实际打印出的端口：

```bash
export PYTHONPATH="$PWD/examples"
TASKFORGE_DEMO_DIR=$(mktemp -d)
uv run --locked taskforge init --database "$TASKFORGE_DEMO_DIR/jobs.sqlite"
printf '{"port":%s,"target":"%s/records.json","transient":true}\n' \
  "$TASKFORGE_DEMO_PORT" "$TASKFORGE_DEMO_DIR" > "$TASKFORGE_DEMO_DIR/input.json"
JOB_ID=$(uv run --locked taskforge submit local.sync --registry local_tasks:registry \
  --database "$TASKFORGE_DEMO_DIR/jobs.sqlite" --input "$TASKFORGE_DEMO_DIR/input.json")
printf '%s\n' "$JOB_ID" "$TASKFORGE_DEMO_DIR"
uv run --locked taskforge worker --registry local_tasks:registry \
  --database "$TASKFORGE_DEMO_DIR/jobs.sqlite" --poll-interval-seconds 0.05
```

Worker 持续轮询。在终端 C（项目根目录）用上一步打印的 UUID 和目录替换两个变量：

```bash
uv run --locked taskforge show "$JOB_ID" --database "$TASKFORGE_DEMO_DIR/jobs.sqlite"
uv run --locked taskforge history "$JOB_ID" --database "$TASKFORGE_DEMO_DIR/jobs.sqlite"
uv run --locked taskforge list --database "$TASKFORGE_DEMO_DIR/jobs.sqlite"
cat "$TASKFORGE_DEMO_DIR/records.json"
```

最终 Job 为 SUCCESS，两次 Attempt 的类别依次为 TASK_ERROR、无错误。
查询不需要设置 `PYTHONPATH` 或指定注册表。终端 B 用 Ctrl-C 等待 Worker 退出；
终端 A 同样停止服务。确认输出后删除本例的 `input.json`、`records.json`、`jobs.sqlite`
及 `jobs.sqlite.lock`，再移除空的 `TASKFORGE_DEMO_DIR`。完整自动验收脚本已包含回收流程。

注册表 `local_tasks:registry` 中其余任务可同样提交：

| 任务名称 | JSON 输入字段 | 业务语义 |
| --- | --- | --- |
| `local.sync` | `port`、`target`、可选 `transient` | 将固定上游记录覆盖写入小文件 |
| `local.backup` | `port`、`target` | 将固定上游内容覆盖写入备份文件 |
| `local.maintenance` | `directory` | 删除该演示目录中的普通 `*.tmp` 文件 |
| `local.timeout` / `local.stop` | `started`、`cleaned` | 可控等待与协作清理，用于超时和停机验收 |
| `local.ledger` | `database`、`idempotent`、可选 `value`（默认 7） | 在独立业务 SQLite 中提交记账副作用 |

所有路径应指向自己的演示目录；父目录须存在。示例本地文件与业务 SQLite 写入是极小、
有界的同步操作，网络等待使用真正的 asyncio 流。大型备份、生产数据库或阻塞 API
应使用合适的异步客户端或在用户代码中管理线程与取消，不能阻塞 Worker 的事件循环。

## 成功 commit 前崩溃的证据

`verify_scenarios.py` 只在崩溃验收子进程中安装一个屏障：用户函数已返回、SUCCESS SQL
已 flush 后暂停 `_commit`。其他事务仍使用真实 SQLite 和原有 `_commit`；核心文件未修改。
父进程读取屏障中的 Job ID/attempt_number，再通过真正的 CLI show/history 观察
已提交状态仍为 RUNNING，并从独立业务数据库读到已经提交的副作用，随后发送 SIGKILL。
这不是模拟完成结果；退出后再次查询证明未提交的 SUCCESS 回滚，而业务副作用仍存在。

新 CLI Worker 取得原数据库锁、确认中断并安排重试。第二次领取不早于中断完成加
0.1 秒，Job ID 不变，number 变为 2。两组业务数据库分别使用独立文件：

| 业务方式 | 实际函数调用 | 已提交副作用 | 合计 value |
| --- | --- | --- | --- |
| 普通 INSERT | 同一 Job，attempt 1 和 2 | 两条 | 14 |
| Job ID 唯一键 + INSERT OR IGNORE | 同一 Job，attempt 1 和 2 | 一条 | 7 |

`local.ledger` 的幂等键和副作用是同一业务事务中的同一条 effects 记录；calls 表只用于
证明第二次函数确实运行。不能先写“已处理”标记、再执行无法原子提交的外部操作，
否则两步间崩溃会丢失效果。远程 API 须支持自己的 idempotency key 或其他业务一致性协议。
稳定 Job ID 只覆盖同一 Job 的重试与恢复；再次 submit 产生新 ID，需要业务去重时应采用
自己的稳定业务键。核心的 Job SUCCESS 与业务数据库仍分属两个事务，没有恰好一次保证。

原始收据在 [verification-t10.json](verification-t10.json)，包含崩溃前后快照、两次调用、
副作用、真实退出码及源码 SHA256。收据中的 `<temporary>` 是已清理目录的占位符；
它是本次历史证据，后续合法代码变更应重新验收并新增记录，不把旧指纹当成修改禁令。
