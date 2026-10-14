# GitHub G05 复现与证据

G05 的七来源真实基线/重复刷新、固定历史重放和正常/故障成对对照已经执行。
最终质量与分发验收状态以 [verification.md](verification.md#g05真实接入与最终交接) 和
[verification-g05.json](verification-g05.json) 为准；下面的步骤分别复现三组证据。
生产命令、字段及检查点契约仍由 [README](../README.md#github-项目与-issue-采集) 维护。

## 1. 环境与证据入口

在项目根目录使用本机 CPython 3.12、既有锁文件和已授权的 github extra：

```bash
UV_OFFLINE=true UV_CACHE_DIR="$PWD/.cache/uv" uv sync --locked --extra github
```

真实联网命令沿用代理和证书环境，通过 `resolve_credential` 在内存读取既有凭据。
需要 GitHub 仓库只读权限；本次七来源均读取成功，未测试任何写权限。
默认 pytest 仅使用受控 HTTP 和假凭据。WSL2/Linux 默认沙箱曾限制 aiosqlite 跨线程唤醒及
`gh` 认证访问；本次真实数据库/子进程及网络在自动审批允许的沙箱外环境执行。

三个独立入口均位于业务目录，进入 sdist；生产 registry 不导入验收工具。
下面使用 `.taskforge/g05/reproduce/` 作为新目录。再次复现时换用另一空目录；尤其 comparison
拒绝覆盖已有实验状态。数据库与原始 HTTP 缓存留在 `.taskforge/`，公开证据位于
[github-evidence/](github-evidence/)，只包含必要字段、指纹、相对路径及固定链接。

## 2. 七来源真实基线及新 UUID 刷新

```bash
uv run --locked --extra github python -m integrations.github.acceptance live \
  --state-directory .taskforge/g05/reproduce/live \
  --output .taskforge/g05/reproduce/evidence/initialization --scenario initialization
uv run --locked --extra github python -m integrations.github.acceptance live \
  --state-directory .taskforge/g05/reproduce/live \
  --output .taskforge/g05/reproduce/evidence/natural_update --scenario natural_update
uv run --locked --extra github python -m integrations.github.acceptance audit \
  --state-directory .taskforge/g05/reproduce/live \
  --output .taskforge/g05/reproduce/evidence/remote-audit.json
```

`live` 调用正式 `commands.initialize/submit`，每来源使用新 UUID，启动原核心 CLI Worker，
以公开 get 查询终态，再发送 SIGTERM、等待进程回收，最后调用 export 更新核心快照。
失败保留状态和收据，命令非零退出；调查关联与失败原因后才开始新意图。
生产策略仍为 2 次额外重试、5/60 秒退避、300 秒单次超时。

完成标准：两组各七个 `SUCCESS` Job、完整业务批次、终态公开收据；随后 audit 覆盖七来源。
本次 [初始化命令记录](github-evidence/initialization/run.json)、
[重复刷新命令记录](github-evidence/natural_update/run.json) 分别链接同目录 submissions 和
逐刷新 JSON；各目录 `indexes/` 只列出该次运行的收据。
[远端核验](github-evidence/remote-audit.json) 独立核对仓库 ID、默认分支、固定 Commit、
Release 全部可见身份及已保存字段指纹、普通 SKILL.md 完整清单和 blob SHA、Issue 全字段。
可变 Issue 若在采集后更新，核验明确记录新旧 UTC 与不同指纹；本次 98 个 Issue 与核验时远端相等，2 个已在采集后更新；全部 100 个保存内容
另与实际采集时的持久 HTTP payload 指纹相等，且满足各自 UTC 上界。

| 来源 | 远端 repository ID | 初始实体/版本 | 重复后的实体/版本 | 重复变化 |
| --- | --- | --- | --- | --- |
| mattpocock/skills | 1148788086 | 29 / 29 | 29 / 29 | 0 |
| addyosmani/agent-skills | 1158722119 | 30 / 30 | 30 / 30 | 0 |
| yetone/magpie | 1383809012 | 30 / 30 | 30 / 30 | 0 |
| tt-a1i/archify | 1211139949 | 30 / 30 | 30 / 30 | 0 |
| deepseek-ai/deepseek-harness | 1333065091 | 30 / 30 | 30 / 30 | 0 |
| morluto/rea | 1209966933 | 30 / 30 | 30 / 30 | 0 |
| openai/codex | 965415649 | 100 / 100 | 100 / 101 | 1 次内容更新 |

初始窗口为最多 20 Commit、10 Release 内容、100 匹配 Issue；不表示完整历史。
技能清单分别为 38、25 个普通文件，均排除 1 个 symlink，未观察到 submodule。
Codex 基线扫描 2 页、103 项，排除 3 个 PR，选择 100 个 Issue，列表尚未穷尽；
重复刷新使用前次 U 减 60 秒的 `since`，完成 1 页，扫描 3 项、排除 1 个 PR，
保存 2 个匹配快照，其中 1 个内容改变。默认空筛选不限范围；非空标签/关键词/编号组合和
退出筛选的行为由 G03 受控测试证明，本次未把非空筛选的联网采集计为成果。
所有保存快照的 `updated_at ≤ U`，增量检查点推进到请求前固定的 U。

生产每次尝试仍最多 100 请求、每列表 10 页。audit 是独立的单项内容核验，不提交业务或
推进检查点；它需要 100 个 Issue GET 加仓库元数据，所以专用预算为 200 请求。
调整理由与预算 SHA-256 在核验文件中；其余预算未改。
audit 再次执行时仅复用 checkpoint 完全一致的已完成来源核验，未完成来源重新检查。

首次真实接入发现 GitHub Link 的 numeric repository 路径及 `after` 游标被原校验拒绝，
Codex 三次尝试失败且检查点为空；[失败证据](github-evidence/initial-failure.json) 单独保留。
修复后用新隔离目录重新建立七来源基线，没有增大生产预算。分页只允许核实仓库 ID 后
从 alias 切到同一 numeric endpoint；保持筛选、per_page、页序与游标推进校验及实际请求预算。
8 个新增测试覆盖真实 Link 形态、304 重用、身份/endpoint/筛选冲突、重复参数、游标循环和预算。

## 3. 两技能来源固定真实历史重放

```bash
uv run --locked --extra github python -m integrations.github.acceptance history \
  --state-directory .taskforge/g05/reproduce/history-matt \
  --output .taskforge/g05/reproduce/evidence/history-matt --source mattpocock-skills \
  --sha 1bb95954ef0d06ba4d64a9c267fb75f57c614a1f \
  --sha 49dd158d1076134a641b33efb035946536778336 \
  --sha 49dd158d1076134a641b33efb035946536778336
uv run --locked --extra github python -m integrations.github.acceptance history \
  --state-directory .taskforge/g05/reproduce/history-addy \
  --output .taskforge/g05/reproduce/evidence/history-addy --source addyosmani-agent-skills \
  --sha 62fcdcd8ab07866e0a895234cf13505fb18c5419 \
  --sha 3b9097e7f56a06c8d32276d7baaa56004523e0c7 \
  --sha bf0608227afc1303d7cc7506413c651d45d66dab \
  --sha bf0608227afc1303d7cc7506413c651d45d66dab
```

`history` 在隔离数据库持来源锁，用原 `GitHubHTTP/collect_skills` 获取真实 Commit/tree，
保存完整快照、差分和业务批次，导出 `historical_replay` 收据。它只验收技能定义，
不改变生产 `RefreshInput` 的默认分支契约，也不把历史标签当作实验本身。
完成标准：两个来源都有真实增删改及同 SHA 零变化；逐阶段 `history.json` 提供完整文件清单、
tree SHA、parent、固定链接、排除边界和原始收据，不能以只存在文件作为完成依据。

[mattpocock 重放](github-evidence/historical_replay/mattpocock-skills/history.json)：
35 → 38 个普通定义，增 4、改 19、删 1，第三次同 SHA 为 0 变化。
[真实 Compare](https://github.com/mattpocock/skills/compare/1bb95954ef0d06ba4d64a9c267fb75f57c614a1f...49dd158d1076134a641b33efb035946536778336)
可核对真实前进历史，删除路径为 `skills/engineering/resolving-merge-conflicts/SKILL.md`。

[addyosmani 重放](github-evidence/historical_replay/addyosmani-agent-skills-pr665/history.json)：
1 → 26 个定义，增 25、改 1；真实父子提交随后改 6、删 1；最后同 SHA 为 0 变化。
[选择依据与 PR 状态](github-evidence/historical_replay/addyosmani-agent-skills-pr665/selection.json)
记录公开 [PR #665](https://github.com/addyosmani/agent-skills/pull/665) 当时 open、未合并。
默认及公开分支的 704 个可达提交没有普通 SKILL.md 删除，故选用该仓库公开 PR 的真实历史，
删除 `skills/requirement-traceability/SKILL.md`，
[父子 Compare](https://github.com/addyosmani/agent-skills/compare/3b9097e7f56a06c8d32276d7baaa56004523e0c7...bf0608227afc1303d7cc7506413c651d45d66dab)
可核对；没有倒序树或合成历史。
[远端祖先证明](github-evidence/historical_replay/ancestry.json) 保存三段真实 Compare 的
status/base/merge-base；它证明祖先关系，未声称采集全部 Commit 增量。
默认分支另有[补充重放](github-evidence/historical_replay/addyosmani-agent-skills/history.json)，
1 → 25 个定义，增 24、改 1、无删除。生产基线仍只读默认分支。

所有目标树完整，均通过 recursive 获取；symlink/submodule 不展开，实际排除数见各阶段。
本次真实树未截断且无 submodule；截断补齐与失败时不推断删除由 G02 受控测试证明。
技能定义不安装、不执行。公开 Issue 未为验收修改。

## 4. 正常/故障各三次 direct 成对对照

已有[固定输入](github-evidence/comparison-input.json) 来自真实 mattpocock/skills 的固定提交，
仅保留相关 API 字段，删除 Commit 作者/提交者姓名和 email。直接重放固定输入不访问 GitHub：

```bash
uv run --locked --extra github python -m integrations.github.comparison compare \
  --state-directory .taskforge/g05/reproduce/comparison \
  --fixture docs/github-evidence/comparison-input.json \
  --output .taskforge/g05/reproduce/evidence/comparison
```

需要另采当前输入时显式联网运行以下命令；它与已保存固定输入的对照不是同一个实验：

```bash
uv run --locked --extra github python -m integrations.github.comparison capture \
  --state-directory .taskforge/g05/reproduce/capture \
  --fixture .taskforge/g05/reproduce/new-comparison-input.json
```

[预先固定的协议](github-evidence/comparison/protocol.json) 指定双方相同业务代码、请求门控、
预算及隔离状态，三次交替执行顺序。TaskForge 用正式提交和原核心 CLI Worker；direct 用正式
业务 CLI。同源 API flock 和 `GitHubHTTP` 保持生产实现，只有 HTTP transport 读取固定本地输入。
采集/核心/数据库未替换；仅验收子进程在第一次导出 os.replace 前设置 stdin 屏障。

故障前确认业务已提交、核心仍 RUNNING；SIGKILL 后等待回收，再启动同一 Worker 命令，
或以原 refresh_id 重跑 direct。TaskForge 保存同 Job 的 INTERRUPTED/SUCCESS；direct 用
新的执行身份核对原完成标记。双方都只重建导出，实际 HTTP 均仍为 6 次，业务未重复变化。
所有进程在 finally kill/communicate 回收。收据重导出后才检查核心终态。
先比较 checkpoint、完整实体、技能清单、变化及实体/版本计数，再比较耗时。

[原始对照数据](github-evidence/comparison/comparison.json) 包含 6 对/12 次运行的 UTC、
退出码、总/采集/排队/恢复时间、尝试、逐请求记录、人工与编排操作、输入/源码/业务指纹。
各运行目录保存 `run.json`、`semantic.json`、终态 receipts 和 indexes。

| 场景（每组 n=3，中位数秒） | TaskForge 总耗时 | direct 总耗时 | TaskForge 采集 | direct 采集 |
| --- | --- | --- | --- | --- |
| 正常 | 0.698256 | 0.487496 | 0.070260 | 0.072552 |
| 业务 commit 后 SIGKILL/恢复 | 6.035426 | 0.754977 | 0.066925 | 0.068230 |

TaskForge 正常/故障组排队中位数为 0.340880/0.312063 秒；direct 无持久队列，记 0。
故障恢复时间 TaskForge 中位数约 5.545 秒、direct 约 0.356 秒；双方各 1 次恢复尝试，
每次故障由脚本执行 SIGKILL 和一次同命令重启，人工操作为 0。
总计时从 submit/direct 启动至成功及进程退出，排除共同初始化和终态重导出；采集计时采用
生产 coverage.elapsed_seconds，截止业务 apply 前。TaskForge 使用默认 5 秒持久退避，
direct 按协议立即重启，故恢复等待不同。该对照说明默认恢复成本，不预设框架更快；
它不代表真实网络吞吐、所有故障阶段或有统计保证的性能结论。

## 5. 质量、分发与限制

质量检查和实际归档要求沿用 G05 prompt；具体命令/退出码和保护文件核验见验证记录。
sdist 必须包含业务、全部 tests、README、文档和全部公开 JSON/Markdown 证据，并与当前
文件逐字节相同；wheel 仍只有核心、py.typed、CLI 及元数据。

当前覆盖 WSL2/Linux、CPython 3.12、单机单 Worker。本次不验证 macOS、更高 Python、
全新联网安装、GitHub 写权限、被删除或私有历史未来的可访问性、完整 Commit/Release/Issue 历史、
完整评论线程、中间事件、技能安装执行、周期调度、Obsidian/RSS 或 Web UI。
REST 分页不是冻结快照；重叠窗口减少遗漏但不提供恰好执行一次。
PR 固定 SHA 未来不可访问时，真实重放应报失败并保留待验收状态，不能用本地替身宣称远端成功。
官方契约复核：[版本](https://docs.github.com/en/rest/about-the-rest-api/api-versions)、
[分页](https://docs.github.com/en/rest/using-the-rest-api/using-pagination-in-the-rest-api)、
[原生树](https://docs.github.com/en/rest/git/trees)、[Issues](https://docs.github.com/en/rest/issues/issues)。
生产 API 根地址及 `2026-03-10` 保持固定。
