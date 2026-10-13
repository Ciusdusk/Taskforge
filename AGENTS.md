# TaskForge 项目约束

## 代码生成与修改

本项目所有代码生成与修改，包括源码、测试、脚本和文档中的代码示例，必须使用
`karpathy-guidelines` skill。开始代码工作前，读取并应用
[/home/xciusdusk/.agents/skills/karpathy-guidelines/SKILL.md](/home/xciusdusk/.agents/skills/karpathy-guidelines/SKILL.md)。
若路径失效，先查找该 skill；无法找到时停止代码生成并说明缺失项。

## 项目事实与会话交接

- 需求、功能边界和架构以 `docs/architecture.md` 为准。
- 当前任务范围、每个任务一个会话的约定及交接顺序以 `docs/implementation-plan.md` 为准。
- 实际验收状态以 `docs/verification.md` 为准；`docs/verification-t01.json` 保留 T01 验收时的
  检查结果和文件指纹，仅作为历史证据。
- 新会话按 `docs/prompts/` 中对应任务的 prompt 检查前置条件并完成该任务。
