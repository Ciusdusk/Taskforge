# TaskForge GitHub 接入实施计划

登记日期：2026-10-14。状态：G01—G03 已验收；G04—G05 待实现、待验收，整体真实接入待验收。

本文由用户提供的接入草案落盘，作为 GitHub 接入设计细则。架构边界见
[architecture.md](architecture.md)，任务范围与交接顺序见
[implementation-plan.md](implementation-plan.md)，实际验收以
[verification.md](verification.md) 为准；G01—G03 已完成，下一独立会话从 [G04 prompt](prompts/G04.md) 继续。

## 1. 目标、依据与边界

将七个 GitHub 来源接入现有 TaskForge，形成手动提交、持久执行、增量采集、业务去重和结果核验的完整链路。

| 来源 | 采集内容 |
|---|---|
| mattpocock/skills、addyosmani/agent-skills | 默认分支提交、Release、SKILL.md 定义变化 |
| yetone/magpie、tt-a1i/archify、deepseek-ai/deepseek-harness、morluto/rea | 默认分支提交、Release |
| openai/codex | Issue 基线、新增、更新、关闭与重开；排除 PR |

草案准备阶段记录了核心接口、示例、依赖及官方 API 文档调查。实施准备时核对了项目文档、核心公开接口与配置，未重新执行核心验收或联网核验。
G01 基础能力、G02 项目/技能及 G03 Issue 采集现已验收，具体证据见验证文档；真实 GitHub 接入仍待 G05 验收。

用户已确认采用 httpx.AsyncClient，Release 内容更新覆盖已保存记录与后续新增记录。运行限定 WSL/Linux 本地文件系统，不依赖 Obsidian 接入，不增加周期调度、Web UI、LLM 摘要、技能执行或完整评论线程采集。

## 2. 接入结构与接口

### 业务模块、配置与入口

- 业务代码放在 integrations/github/，分为配置、HTTP、业务存储、采集、任务注册和命令入口；核心源码与公共接口保持现状。
- 注册表使用 integrations.github.tasks:registry，注册单个任务 github.refresh；按输入来源类型分派项目或 Issue 采集。
- 使用项目源码运行业务模块，并加入 sdist；wheel 继续分发现有核心包和 CLI。
- 新增 github 可选依赖组，声明 httpx>=0.28.1,<1，由 uv.lock 固定实际版本。实施时先形成依赖和锁文件差异，再确认安装；不主动升级已有依赖。HTTPX 的异步请求和关闭方式遵循官方文档 (https://www.python-httpx.org/async/)。

配置采用 JSON，由 Pydantic 校验并拒绝未知字段。来源包含稳定 source_id、kind、owner/repo、技能扫描开关和可选 Issue 筛选。默认配置仅包含上述七个来源，不自动发现仓库。

业务命令通过 python -m integrations.github 提供：

| 命令 | 行为 |
|---|---|
| init | 创建业务目录，初始化两份数据库，登记来源配置 |
| submit | 提交指定来源或全部来源；输出来源、refresh_id 和 Job ID |
| reconcile | 核对尚未确认的提交，修复业务批次与 Job 的关联 |
| show | 查看来源检查点、批次结果、覆盖范围及核心 Job/Attempt |
| export | 从数据库重建 JSON 收据和 Markdown 索引 |
| direct | 调用同一采集逻辑，供隔离的直接执行对照实验使用 |

Worker 使用现有 TaskForge CLI 与注册表加载方式；业务入口不另写 Worker、重试或信号处理逻辑。submit --rebaseline 是显式建立新业务基线，不属于核心人工重放。

### 输入、身份与存储

任务输入模型固定包含：

- refresh_id：提交前生成的 UUID，重试保持不变。
- state_directory：规范化的业务目录绝对路径。
- source：来源配置和采集预算的非敏感快照。
- source_generation：提交时的来源配置代次。
- operation：refresh 或 rebaseline。

任务函数接收单个模型并返回 None；Job ID、尝试号从 get_execution_context() 获取。采集函数与任务包装分离，直接执行传入独立执行身份，复用相同业务逻辑。

默认运行目录为项目内已被忽略的 .taskforge/github/，包含核心任务数据库、独立业务数据库、锁、收据和索引。支持显式指定其他 Linux 本地目录。

业务数据库使用已有 aiosqlite，显式 schema 版本、短事务和有界锁等待。保存来源代次与检查点、提交意图、批次与尝试、当前业务记录、变化记录、技能快照、Release 身份清单、HTTP 缓存和请求等待时间；不访问核心私有表。

唯一身份采用：

| 对象 | 唯一身份 |
|---|---|
| 提交批次 | refresh_id |
| Commit | 仓库远端 ID、SHA |
| Release、Issue | 仓库远端 ID、对象远端 ID |
| 技能变化 | 仓库远端 ID、前后快照 SHA、路径 |
| 可变对象版本 | 对象身份、规范化内容指纹；Issue 另保存 updated_at |

来源更换仓库、类型或筛选条件时要求显式重新建立基线；仅调整请求预算不重置基线。旧代次任务不得覆盖新代次检查点，历史记录保留。

### 认证与 HTTP

认证依次读取 GH_TOKEN、GITHUB_TOKEN，最后尝试已有 gh auth token --hostname github.com。原草案记录 gh 存在且凭据读取命令退出成功；实施准备时未重新读取凭据；G01 已复核读取成功且未输出 token，远端有效性仍待验收。凭据只驻留内存，不写入输入、数据库、异常、日志或收据。无可用凭据时在提交前明确失败。

固定使用 https://api.github.com、JSON Accept header 和 X-GitHub-Api-Version: 2026-03-10，不随调用自动切换版本。原草案依据官方支持清单选定此版本 (https://docs.github.com/en/rest/about-the-rest-api/api-versions)；G01 已复核官方支持列表及请求契约，固定版本保持；G05 再做真实兼容性验收，
文档复核不计作七来源联网验收。

默认执行预算：

| 项目 | 默认值 |
|---|---|
| TaskForge 策略 | 最多 2 次额外重试；退避基数 5 秒、上限 60 秒；单次超时 300 秒 |
| Worker | 沿用现有默认配置 |
| HTTP | connect/write/pool 5 秒；read 20 秒；单次请求总等待上限 30 秒 |
| 分页 | per_page=100；单列表最多 10 页 |
| 单来源请求数 | 每次尝试最多 100 次实际 HTTP 请求 |
| 增量 Commit | 最多 500 条 |
| 服务端冷却等待 | 每次尝试累计最多等待 30 秒 |

HTTP 客户端不额外自动重试；失败交给 TaskForge。生产请求沿用现有代理和证书环境，测试客户端隔离这些环境。

同一业务实例所有来源共用请求门控，API 并发固定为 1，并用文件锁覆盖直接执行与 Worker。请求前核对持久等待时间，换 token 不绕过已有冷却。

条件缓存保存完整请求键、ETag、响应体及分页 Link。收到 304 时复用对应缓存；第一页不变仍须核对后续页。限流等待按 Retry-After、主限流重置时间或二级限流退避记录，持久期限取较晚值。超过等待预算则明确失败，后续尝试在冷却结束前不发实际请求。依据：GitHub 最佳实践 (https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api)。

## 3. 采集、检查点与恢复

### 项目 Commit 和技能定义

每次尝试读取仓库元数据与默认分支，固定目标 commit SHA；后续提交历史和技能树均引用该 SHA。

- 初次保存最多 20 条 Commit，注明有限历史窗口，全部记为初始化记录。
- 后续使用前次成功 SHA 与目标 SHA 调用 Compare，显式分页，核对数量与完整性。
- SHA 相同表示提交无变化；普通前进采集增量。历史分叉、回退、旧 SHA 不可比较或超过 500 条时保留旧检查点，标记需要重新建立基线。
- 仓库路径重定向后核对远端仓库 ID；身份发生变化不得沿用旧基线。

Compare 的文件列表最多覆盖 300 个变化文件，因此技能检测单独使用完整树快照。依据：Commits API (https://docs.github.com/en/rest/commits/commits#compare-two-commits)。

两个技能来源扫描固定提交原生 Git tree 中的普通 SKILL.md 文件，保存路径和 blob SHA。递归树截断时改为逐子树遍历；任何子树不完整或达到预算，都使本次采集失败，不产生删除判断。子模块和符号链接不作为外部技能定义展开，收据注明边界。依据：Trees API (https://docs.github.com/en/rest/git/trees#get-a-tree)。

前后清单比较产生新增、修改、删除；重命名表示删除加新增。记录链接固定到对应提交，只宣称技能定义变化。

### Release

Release 列表没有更新时间增量参数，因此每次在预算内完整分页核对身份清单。依据：Releases API (https://docs.github.com/en/rest/releases/releases#list-releases)。

- 初次登记全部可见已发布 Release 的 ID，仅保存按 published_at、ID 降序选出的最多 10 条完整记录。
- 后续保存首次观察到的 ID，并持续核对已保存记录的内容；初始化窗口外的旧记录仅保留身份，不监测其内容编辑。
- 完整内容保存 ID、tag、name、body、创建与发布时间、prerelease 和原文链接；以内容指纹识别更新，不假设 Release 提供 updated_at。
- 包含 prerelease，排除 draft；没有 Release 是正常结果。普通 tag 和附件下载不在范围内。
- 身份清单未分页完成时不推进项目检查点。已关注 ID 消失时保留历史并记录不可见诊断，不直接删除业务记录。

“首次观察到”与“本周新发布”分别统计，避免将新发现的旧发布计为自然新增。

### Codex Issue

使用仓库 Issues API，固定 state=all、sort=updated、direction=desc，通过 pull_request 字段排除 PR。依据：Issues API (https://docs.github.com/en/rest/issues/issues#list-repository-issues)。

- 初次选择最近更新的最多 100 条符合筛选条件的真实 Issue；不足 100 条且已到列表末尾也可成功。明确有限基线，不计为新增。
- 每次尝试记录请求开始时间 U；增量查询使用上次成功检查点减 60 秒作为 since。本地只纳入 updated_at ≤ U 的快照，晚于上界的更新留给下一批。
- 增量必须完成所有分页，随后在同一业务事务中保存记录与检查点 U。中断或预算耗尽保留旧检查点，不使用返回对象的最大更新时间推进。
- 保存业务计划要求的全部 Issue 字段及内容指纹；较旧 updated_at 不覆盖新记录，相同时间的内容变化按较晚观察保存。
- 首次发现的既有 Issue 标为“历史对象首次发现”，不自动计为新建 Issue。状态变化按前后快照记录关闭、重开和其他更新。

筛选在本地完成：标签要求全部命中，关键词在标题和正文中任一命中，编号列表任一命中；不同维度组合采用 AND，空维度不限制。已关注对象不再匹配时仍更新其状态，并记录退出筛选范围，避免标签移除后失联。

G03 实际入口为 issues.refresh_issues，纯收集器为 collect_issues；Issue 检查点包含
repository_id、repository、upper_bound。初始覆盖明确 finite_initial_window/full_history=false
及 list_exhausted；完整增量才穷尽所有分页。筛选使用标签/关键词 casefold，关键词为单字段字面子串。
关注范围由已提交批次按来源代次读取；退出筛选继续关注。变化类型为 initial、added、
first_observed_history、closed、reopened、updated、exited_filter；自然新增仅限创建时间晚于前次上界。
完整字段、可空与规范化规则见 README，真实 SQLite 验收及边界证据见验证文档 G03 节。

REST 分页不提供冻结快照；重叠窗口和身份去重用于减少轮询遗漏。首版只保证保存观察到的状态，不还原两次刷新之间的全部事件，也不声称识别完整评论变化。

### 事务、串行执行与恢复

同一来源使用异步互斥与非阻塞文件锁，覆盖检查点读取、采集、业务提交和批次收据生成；锁文件保留。网络请求期间不持数据库事务。

未提交尝试重试时重新读取最新检查点，并选择本次目标 SHA 或上界；一次尝试内保持目标固定。这样，即使旧 Job 在新 Job 后重试，也不会回写旧检查点。

每个来源的业务记录、完整性标记、检查点和 refresh_id 完成标记同事务提交。项目来源的 Commit、Release 和技能结果共同通过后才提交。失败诊断、请求缓存与冷却状态可以单独保存，但不代表业务采集完成。

- 业务提交前退出：重试重新采集，检查点保持原值。
- 业务提交后、核心 SUCCESS 前退出：重试核对完成标记，重建导出，不再采集或应用业务变化。
- 业务 commit 结果不确定：等待数据库操作实际结束，再以独立连接查询完成标记，禁止直接重做。
- 取消时等待 HTTP、数据库和凭据子进程完成清理，再释放锁并传播取消。

提交入口先在业务数据库保存 refresh_id 和非敏感参数意图，成功后关联 Job ID。提交不确定或进程退出后，reconcile 通过核心 get/list 按 Job ID 或 refresh_id 核对；无法查询确认时保持待核对，不盲目再次 submit。

认证失败、历史异常、预算不足和限流都保存具体业务原因。它们仍按现有核心 TASK_ERROR 策略消耗有限重试预算，不宣称业务永久错误能够立即跳过重试。

## 4. 任务拆分与交接

G01—G05 已登记到 [implementation-plan.md](implementation-plan.md)，每个任务使用独立会话。它们细化 [business-integration-plan.md](business-integration-plan.md) 中 B03 的 GitHub 接入及 B04 的 GitHub 故障、对照与交接部分；Obsidian 备份不在本轮范围内。整体按最多一周安排，验收失败优先修复，不扩大范围。

| 任务 | 依赖与建议时间 | 输出与完成标准 |
|---|---|---|
| G01 配置、HTTP 与业务状态基础 | 现有 T01—T10；第 1 天 | 来源与输入模型、可选依赖、业务 schema、缓存、锁与限流门控；分页、取消、事务回滚、冷却跨重启测试通过 |
| G02 项目与技能采集 | G01；第 2—3 天 | Commit、Release、完整技能快照及变化检测；有限基线、历史异常、内容更新与截断测试通过 |
| G03 Issue 增量采集 | G01、G02；第 4 天 | 有限基线、筛选、重叠窗口、完整检查点与状态变化；分页中断恢复及旧快照测试通过 |
| G04 TaskForge 入口与一致性恢复 | G01—G03；第 5 天 | 注册任务、业务命令、提交核对、收据导出；真实 Worker、重复提交、SIGKILL 和失败隔离通过 |
| G05 真实接入与最终交接 | G01—G04；第 6—7 天 | 七来源真实基线、重复刷新、历史重放、对照收据、完整检查和复现文档 |

任务 prompt 使用 docs/prompts/ 的 G01—G05 对应文件：G01/G02 保留原任务契约，
[G03.md](prompts/G03.md) 保留原任务契约，[G04.md](prompts/G04.md) 已交接，G05 在 G04 验收后补齐。
每个 prompt 指明输入证据、允许修改范围、必要检查、完成标准和下一任务前置条件，并要求读取项目指定 skills。

实施阶段按实际结果更新验证文档及架构状态；保留 T01/T10 历史证据。未通过真实接入前始终标为待验收，不将提案内容计入成果。

## 5. 验收、产物与默认假设

验收分为三组：

- 受控 API 测试：分页与 304 缓存、Release 初始窗口及旧记录编辑、树截断补齐、历史改写、预算耗尽；Issue 的 PR 排除、相同更新时间、上界之外更新、重叠窗口、标签退出、关闭与重开；主/二级限流及重启后的请求抑制。
- 真实持久化与进程测试：同来源互斥、不同来源业务失败隔离、重复尝试和重复 submit、业务事务中断、业务提交后核心 SUCCESS 前 SIGKILL、提交关联中断，以及 HTTP 等待期间的超时和停机清理。
- 真实 GitHub 验收：七个来源分别建立基线并重复刷新，核对远端仓库 ID、SHA、Release ID、技能清单和 Issue 字段。没有合适自然更新时，用固定真实历史 SHA 重放两个技能来源；公开仓库 Issue 不为验收而修改。

正常业务结果保存在业务数据库。JSON 收据按批次重建，Markdown 索引只展示来源状态、覆盖范围、计数、诊断和原文链接；文件采用临时文件加原子替换。导出失败不回滚已提交数据，可由重试或 export 修复。

收据包含 UTC 时间、源码版本与指纹、配置指纹、场景、批次身份、Job/Attempt、前后检查点、请求数、记录变化、耗时和核验结论。初始化、自然更新、历史重放和故障实验分开；实体数与内容版本数分别统计，凭据和个人完整路径不进入公开收据。

直接执行对照使用同一业务逻辑、固定本地输入、相同请求门控和隔离数据库，正常及故障场景各执行三次；记录总耗时、业务耗时、排队等待、恢复尝试和人工操作，不预设框架更快。

各任务运行新增行为测试和既有必要检查；最终运行全量 pytest、Ruff、格式检查、核心及业务模块的 mypy strict、构建和 sdist 内容核验。联网验收单独显式运行，不让默认测试依赖第三方凭据。

默认假设是单机、单 Worker、同一业务实例共用凭据和请求门控，系统 UTC 时钟正常。
真实网络、凭据权限、仓库身份和默认分支留待真实接入验证。G01 安装经用户批准，验收后按用户追加要求提交；
G02 已完成受控项目/技能采集验收，验收后按用户追加要求创建 Git Commit。
G03 已完成受控 Issue 增量验收，新增 96 项行为测试、全量 795 项通过，验收后按用户追加要求创建 Git Commit。
当前基础与采集接口、验收证据见验证文档 G03 节和 G04 prompt；G04—G05 仍待实施。
