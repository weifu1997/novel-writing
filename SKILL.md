---
name: novel-writing
description: "独立完成中文小说的扫榜选题、对标拆文、开书立项、长短篇规划与写作、串行日更、旧稿接管、审稿返修和百万字连续性管理，并以确定性字数门禁、文风契约和事务状态库控制质量。适用于番茄、七猫、起点等平台方向；不用于非虚构文案，也不把榜单与平台经验当成爆款公式。"
---

# 小说写作

把用户的意图、项目事实和既定文风落实成可用的小说产物，并能从空白项目或冻结的旧稿建立可靠工作基线。优先保证故事成立、人物可信、字数合约准确，再考虑平台偏好。不要承诺“爆款”“真人率”或规避平台检测。

## 先锁定本轮范围

识别四项：操作（扫榜/拆文/立项/规划/写章/日更/续写/改写/接管旧稿/审稿/返修）、对象、交付范围、停笔点。用户已给足信息时直接执行；只有缺失项会实质改变作品时才追问，且只问必要项。

- 只讨论结构时不建项目、不写正文。
- 只要大纲时不自动扩成细纲或正文。
- 写第 N 章时只处理该章；未授权时不批量续写。
- 只要求接管评估时不改原稿、不建立正式事实源；只要求审稿时不直接返修。
- 改写和去 AI 味默认不改变剧情事实、人物关系、视角和伏笔。
- 当前请求 > 已确认项目事实 > 本书文风契约 > 平台 profile > 通用建议。发生冲突时按此顺序裁决并说明。

## 按任务加载

不要一次性加载全部资料。先读取 [references/workflow.md](references/workflow.md) 中当前模式，再按下列条件精确加载：

- 题材选择、题材融合或题材成色不足：读取 [references/genre-craft.md](references/genre-craft.md)，再按其路由只加载一个主题材模块；混合题材最多摘取一个辅模块的相关条目。
- 规划约 30-150 章、制作卷纲或阶段滚动计划：读取 [references/volume-planning.md](references/volume-planning.md)。
- 用户提供对标作品、要求拆书或需要从样本提炼功能：读取 [references/comparative-analysis.md](references/comparative-analysis.md)。
- 用户要求榜单、市场趋势、商业选题或近期平台方向：读取 [references/market-scan.md](references/market-scan.md)。公开起点移动端样本可用 `scripts/market_fetch.py`，结果必须再过 `market_sample.py`。只有进入完整对标拆文时再读取 [references/deconstruction-pipeline.md](references/deconstruction-pipeline.md)。
- 用户要求把一本长篇或短篇系统拆成可复用资产、续跑既有拆文，或需要建立拆文库供后续召回：读取 [references/deconstruction-pipeline.md](references/deconstruction-pipeline.md)。
- 接管、迁移或续写已有正文且尚无可信基线：读取 [references/legacy-import.md](references/legacy-import.md)。
- 审稿、诊断质量或按问题返修：读取 [references/review-protocol.md](references/review-protocol.md)。
- 创作短篇、短故事、单篇或分节但以全文闭环的作品：读取 [references/short-fiction.md](references/short-fiction.md)。短篇不套长篇卷规划与日更协议。
- 用户明确授权一次写多章、日更或第 N-M 章：读取 [references/daily-batch.md](references/daily-batch.md)，并继续按章加载正文、连续性和章法资料。
- 设计章纲、写章或审查章节组织：读取 [references/chapter-craft.md](references/chapter-craft.md)，再按其路由只加载场景底座和一张主章法卡；必要时摘取一张辅卡。
- 创建或修改正文：读取 [references/prose-craft.md](references/prose-craft.md)。
- 长篇续写、跨章修改或项目含既有设定：读取 [references/continuity.md](references/continuity.md)。
- 计划超过 50 万字、超过 150 章、多人协作，或用户要求高连续性保障：再读取 [references/long-project-engine.md](references/long-project-engine.md)。计划超过 100 万字时必须使用其中的结构化状态流程。
- 用户指定番茄、七猫、起点、短篇渠道，或询问平台适配：读取 [references/platform-profiles.md](references/platform-profiles.md)。平台规则、收益和算法数字属于时效信息，必须查官方当期来源；本地 profile 只提供写作侧假设。

优先复用现有项目结构和权威文件。若项目已经使用 `oh-story` 的结构化追踪或其他可验证状态系统，不另建一套平行记忆；普通 Markdown 备忘录不等同于结构化状态系统。迁移既有项目按 `legacy-import.md` 从 accepted corpus 重建基线，不能把计划当事实。

## 超长篇稳定性

百万字项目不能依赖会话记忆或自由格式摘要。数据库项目以 `追踪/story-state.sqlite3` 为已接受事实源，每章必须完成“最终正文与记录 -> 含字数合约和原文证据的变更单 -> `story_state.py check` -> `commit`”。`check` 和 `commit` 都会对当时的正文重新计数；提交失败时正文快照、章节记录和全局状态均不得推进。

修改旧章前必须运行 `impact`；连续性变化后，下游章节全部复核或重提，`verify` 清零 `needs_review` 前不得续写。达到卷末时检查 `commit` 返回的持久化 `volume_audit`；引擎会阻止在未解决 blocker 时开启后续卷。写前运行 `story_state.py context`（可无 ID）加载热实体、未关闭线索和作者/读者分栏时间线；`追踪/视图/` 是只读派生。只加载本章相关状态，不把数据库全量塞入上下文。

这套机制保证流程可验证、冲突可阻断、提交可回滚，不保证文学作品绝对零错误。不得宣称“百万字零穿帮”；应报告门禁结果、未解决债务和启发式审计的边界。

## 字数是硬合约

用户给出的范围按原意执行：

- “2500-3000 字”“不少于 2500”“不超过 3000”按明确边界验收。
- “约 3000 字”且未另给容差时，使用 2850-3150（+-5%）。
- 用户或平台给出计数口径时服从该口径；否则使用 `visible_chars_v1`：去掉可识别 YAML frontmatter、首个 Markdown 或 `第N章` 纯文本章节标题和 Markdown 装饰后，统计正文所有非空白可见字符，标点计入。明确要求“纯汉字数”时使用 `--metric han_chars`。同一项目锁定口径，不在章节间切换。

从当前已加载的 `novel-writing` skill 目录定位脚本，不硬编码仓库路径。正文落盘后必须运行：

```bash
python "<skill-dir>/scripts/chapter_guard.py" <正文文件> --min <下限> --max <上限>
```

纯汉字口径在命令末尾追加 `--metric han_chars`。

若用户只给约数，运行：

```bash
python "<skill-dir>/scripts/chapter_guard.py" <正文文件> --target <目标> --tolerance 0.05
```

`under` 时只展开细纲已批准但呈现不足的行动、选择、阻力、反应或后果；`over` 时先删重复解释、同义反应、无功能动作和提前总结。每次修改后重测，同一版本最多做 3 轮针对性收口。仍未通过时停止改正文：说明差额，并让用户在“修改章纲/调整范围/接受当前长度/放弃本版”中裁决。不得注水或删掉必要内容硬凑；除非用户明确接受带外长度，否则不得称为完成。不要相信模型自报字数。

## 文风不是禁词分数

写正文前建立本轮 `Style Contract`：视角与叙事距离、叙述声口、句段节奏、对话声纹、意象来源、信息留白、明确禁忌。优先从用户自己的样章和已确认章节提取；没有样章时采用克制的题材默认值，并把它当可调整假设。

正文完成后运行：

```bash
python "<skill-dir>/scripts/prose_lint.py" <正文文件>
```

该脚本只定位可能的套式句、解释腔、库存动作和节奏过齐，不计算“AI 概率”。逐条回到语境判断：有角色声线或叙事功能的保留，确实重复、替读者总结或脱离视角的才局部返修。禁止为“像人”故意加错字、随机口语、无关感官、脏话或参差句长。

## 交付门禁

正文交付前同时满足：

1. 必发生、禁止发生、视角、时间锚、停笔点和章尾承诺逐项通过。
2. 字数脚本状态为 `pass`；若用户明确接受带外长度，先把结构化项目变更单的字数合约改成获准边界，再提交。
3. 每个主要场景至少改变一项状态：信息、关系、目标、风险、资源或情绪位置；纯气氛章也要改变读者理解。
4. 人物只能依据其已知信息行动；设定、时间线、伤势、物品和未回收线索无冲突。
5. lint findings 已按语境复核，不把启发式命中机械改坏。
6. 最终版本通过后才更新连续性账本；正文文件不混入提示词、检查表、字数报告或写作术语。
7. 结构化项目的 `story_state.py check` 与 `commit` 已通过，工作副本需要交付时 `verify` 为 pass；卷末自动审计无未裁决 blocker。

向用户简要报告文件、实际计数字数、约束结果和仍需其裁决的事项。不要用长篇自评挤占正文。
