# 超长篇状态引擎

本协议用于计划超过 50 万字、超过 150 章、多人协作，或用户明确要求高连续性保障的项目。计划超过 100 万字时必须使用；已有 `oh-story` 或其他结构化权威状态的项目继续沿用原协议，除非用户授权迁移，不能同时维护两套事实源。

## 能保证什么

`scripts/story_state.py` 使用 Python 标准库 SQLite，提供：

- 人物、物品、地点、势力、时间线、伏笔/承诺/问题/剧情债务的结构化状态。
- 基于当前正文证据的语义变更单；工具计算 before/after、检查前置值与引用完整性。
- 字数合约在 `check` 与 `commit` 时重新测量，合格结果和正文哈希随章节快照保存。
- 正文快照、逐章记录快照、全局状态和卷末审计在一个 SQLite 事务中成为“已接受版本”。
- 单头回滚、Markdown 工作稿漂移检测和安全 checkout。
- 字段级读写依赖、旧章修改影响传播和待复核章节阻断。
- 可复现的卷级人物弧、剧情债务、时间线、待复核状态和文风漂移审计；未解决 blocker 会阻止开启后续卷。

它不能保证文学作品绝对零错误，也不能仅靠正则从正文可靠理解隐含语义。Agent 负责从最终正文提取带原文证据的候选变化，工具负责确定性校验、提交和回滚。不得把“脚本通过”描述成剧情一定正确。

## 权威边界

数据库固定为：

```text
作品根/追踪/story-state.sqlite3
```

数据库内的 `chapter_versions`、当前资源表和提交链是已接受事实源。磁盘上的 `正文/*.md` 与 `追踪/逐章记录/*.md` 是工作副本：作者可以继续编辑，但编辑后不自动成为已接受版本。

- `commit` 把正文内容、章节记录内容和状态变化写入同一数据库事务。任一校验或写入失败，三者都不推进。
- SQLite 无法与多个外部 Markdown 文件组成真正的跨文件系统原子事务。因此不要声称 Markdown 文件也被物理原子提交。
- `verify` 检查工作副本是否等于已接受快照。
- `checkout` 显式恢复已接受快照；覆盖前会把不同的工作稿保存到 `追踪/回滚存档/`。
- `rollback` 只回滚当前 head，恢复数据库内的正文版本指针、章节记录版本指针和全局状态。它不擅自覆盖工作稿；需要时再运行 `checkout`。
- 数据库 `project_meta.schema_version` 是库文件版本，当前为 2；工具会把旧库从 1 迁到 2。旧章节会标为 `legacy_unverified`，下次修订必须显式补交字数合约。变更单 JSON 的 `schema_version` 是另一字段，必须为 `DELTA_VERSION`（当前为 1）。不要把库版本 2 填进变更单。

## 初始化

新书建立项目结构后运行：

```bash
python "<skill-dir>/scripts/story_state.py" init --project "<作品根>" --title "<书名>" --json
```

已有正文不能直接空库接着写。按 [legacy-import.md](legacy-import.md) 确定 accepted corpus，并在 staging 中逐章重建人物、物品、地点、势力、活跃线索、时间线、依赖和人物弧；或继续使用项目原有结构化追踪协议。不要把计划中的未来情节导入为既成事实。

## 章节变更单

最终正文和逐章记录完成后，生成一次性 JSON。ID 必须稳定，建议使用 `char.lin-zhou`、`item.copper-key`、`loc.old-home`、`faction.north-gate`、`F001`、`E001`、`ch-001`、`vol-01` 这类命名；改名时保留 ID。

变更单顶层 `schema_version` 必须是 `1`（`DELTA_VERSION`）。这不是数据库的 `SCHEMA_VERSION`（当前为 2）。填 2 会被 `check` 拒绝。

最小骨架：

```json
{
  "schema_version": 1,
  "expected_revision": 0,
  "length_contract": {
    "metric": "visible_chars_v1",
    "minimum": 2500,
    "maximum": 3000
  },
  "chapter": {
    "id": "ch-001",
    "seq": 1,
    "title": "归来",
    "mode": "append",
    "summary": "林舟回到旧宅，决定调查父亲失踪。",
    "continuity_changed": true,
    "volume": {
      "id": "vol-01",
      "seq": 1,
      "title": "旧宅迷踪",
      "planned_end_chapter": 80,
      "status": "active",
      "state": {}
    }
  },
  "entity_changes": [],
  "thread_changes": [],
  "timeline_changes": [],
  "references": [],
  "arc_beats": []
}
```

`expected_revision` 必须取当前数据库返回值。状态在构造变更单后发生变化时，工具会拒绝陈旧提交，必须重新加载上下文并重建变更单。

`length_contract` 是提交协议，不是外部报告。`metric` 只能是 `visible_chars_v1` 或 `han_chars`，上下限为闭区间且至少提供一项。正文或逐章记录为空、正文超出边界，`check` 和 `commit` 都会拒绝；`commit` 不信任之前的 `check` 结果，而是重读当前文件并重新计数。修订默认继承该章上一已接受版本的合约，也可以显式改成用户新批准的边界。

后续章节继续提交当前卷对象。标题、预计结束章、状态或卷级 `state` 变化时，`check` 会在 `volume_change` 中显示 before/after，并与章节一起提交、回滚；卷序号 `seq` 和稳定 ID 不可变。把卷状态改为 `complete` 或写到 `planned_end_chapter` 会触发自动卷审计。

### 人物、物品、地点、势力

创建资源：

```json
{
  "op": "create",
  "id": "char.lin-zhou",
  "type": "character",
  "name": "林舟",
  "aliases": [],
  "state": {
    "role": "main",
    "location_id": "loc.old-home",
    "goal": "查明父亲失踪真相",
    "knowledge": [],
    "faction_ids": []
  },
  "evidence": "林舟站在旧宅门前。"
}
```

更新资源使用 JSON Pointer。人物/物品/地点/势力的动态字段位于 `/state/...`：

```json
{
  "op": "set",
  "id": "char.lin-zhou",
  "path": "/state/location_id",
  "expect": "loc.old-home",
  "value": "loc.archive",
  "evidence": "林舟走进县城档案馆。"
}
```

新增字段用 `"expect_missing": true` 代替 `expect`。删除字段使用 `op=unset` 并提供当前 `expect`。`/type` 是不可变字段，类型纠错应建立正确 ID 并修订引用，不能原地换类。退场但仍需保留历史的对象用 `deactivate`；重新启用用 `reactivate`。停用只把对象移出当前热状态，不删除其存在性，既有时间线和已记录剧情债务仍可引用它；仍处于当前状态的持有者、地点、组织成员等引用必须同时清理或转移。

推荐动态字段：

- `character`：`identity`、`role`、`location_id`、`goal`、`physical_state`、`mental_state`、`knowledge`、`abilities`、`resources`、`relationships`、`faction_ids`。`knowledge` 用对象或列表区分 `knows` 与 `does_not_know`，不要把作者真相写进角色已知。
- `item`：`holder_id`、`location_id`、`quantity`、`status`、`properties`。
- `location`：`parent_id`、`status`、`properties`。
- `faction`：`leader_id`、`member_ids`、`status`、`resources`、`relationships`。

引用字段会校验目标 ID、结构、存在性和适用类型。`relationships` 使用以目标实体 ID 为键的对象，或含 `target_id` 的对象数组；不要塞入无法校验的自由文本。物品同时记录持有人和地点时，地点必须与持有人位置相容。

### 伏笔与剧情债务

`thread_changes` 支持 `foreshadow`、`promise`、`question`、`debt`；状态只能是 `open`、`advanced`、`resolved`、`abandoned`：

```json
{
  "op": "create",
  "id": "F001",
  "state": {
    "type": "foreshadow",
    "summary": "铜钥匙能打开父亲留下的密室",
    "status": "open",
    "importance": "high",
    "introduced_chapter": 1,
    "due_chapter": 40,
    "due_volume_id": "vol-01",
    "last_progress_chapter": 1,
    "state": {"related_ids": ["item.copper-key"]}
  },
  "evidence": "钥匙齿上刻着父亲的名字。"
}
```

推进或回收时用 `set` 修改 `/status`、`/summary` 或其他字段。工具会把 `last_progress_chapter` 更新到当前章。`introduced_chapter <= last_progress_chapter <= 当前章` 是硬约束，不能预写未来章进度。不能只在逐章记录里写“已埋伏笔”而不提交 thread。

### 时间线

`timeline_changes` 的 `sequence` 是本书统一的可排序故事时序，不是章节号。同一时刻使用相同值；需要小数时可以用小数。倒叙、梦境必须明确写 `flashback` 或 `dream`：

```json
{
  "op": "create",
  "id": "E001",
  "state": {
    "sequence": 1,
    "story_time": "故事第一天清晨",
    "mode": "present",
    "fact": "林舟抵达旧宅",
    "location_id": "loc.old-home",
    "participant_ids": ["char.lin-zhou"],
    "state": {
      "reader_knowledge": "林舟抵达旧宅",
      "reveal_status": "revealed"
    }
  },
  "evidence": "天刚亮，林舟推开旧宅院门。"
}
```

`state.reveal_status` 只能是 `hidden`、`partial` 或 `revealed`。省略时视为 `revealed`，`reader_knowledge` 默认等于 `fact`，避免旧数据被误当成未揭示。未揭示事件只进入作者时间线；读者视图只用 `reader_knowledge`。

工具阻断同一角色同一 `sequence` 出现在两个地点，也阻断后续章节的正叙 `sequence` 倒退。确属倒叙时改正 `mode`，不要伪造 sequence 绕过检查。

旧章修订确认某事件从未发生时，用 `delete` 删除当前时间线事实，并提交删除前的完整快照作为乐观锁：

```json
{
  "op": "delete",
  "id": "E001",
  "expect": {
    "sequence": 1.0,
    "story_time": "故事第一天清晨",
    "mode": "present",
    "fact": "林舟抵达旧宅",
    "location_id": "loc.old-home",
    "participant_ids": ["char.lin-zhou"],
    "state": {}
  },
  "evidence": "林舟并未在那个清晨抵达旧宅。"
}
```

当前时间线表会移除该事件，提交历史保留 before/after 墓碑，回滚可恢复。`delete` 只用于时间线；实体退场使用 `deactivate`。

### 引用与人物弧

正文使用但未改变某项状态时，在 `references` 声明读取依赖。这样旧章修改才能找到受影响章节：

```json
{
  "kind": "entity",
  "id": "char.lin-zhou",
  "fields": ["/state/knowledge"],
  "reason": "本章判断依赖角色已知信息",
  "evidence": "他记得父亲从不让人碰那只木箱。"
}
```

主要人物发生目标、信念、关系、身份、能力或道德选择变化时写入 `arc_beats`：

```json
{
  "character_id": "char.lin-zhou",
  "dimension": "goal",
  "beat": "从逃避旧宅转为主动调查",
  "before": "逃避",
  "after": "调查",
  "evidence": "这一次，他没有转身。"
}
```

`evidence` 必须是最终正文中的短原文，不接受大纲计划、推测或改写后的近义句。一次变化只提交一次；没有真实变化时保持空数组。`revision` 和 `revalidate` 的 `references`、`arc_beats` 都是该章节当前版本的完整集合，不是相对上一版本的增量；新版本提交后，旧版本的依赖与人物弧不再参与影响分析或卷审计。

## 每章提交闭环

先完成字数门禁与文风复核，再执行：

```bash
python "<skill-dir>/scripts/story_state.py" check \
  --project "<作品根>" --body "<正文文件>" \
  --record "<逐章记录>" --delta "<变更单.json>" --json
```

`check` 通过后才提交：

```bash
python "<skill-dir>/scripts/story_state.py" commit \
  --project "<作品根>" --body "<正文文件>" \
  --record "<逐章记录>" --delta "<变更单.json>" --json
```

提交成功后删除临时变更单。不要手改 SQLite。定期执行：

```bash
python "<skill-dir>/scripts/story_state.py" verify --project "<作品根>" --json
```

若本章达到卷的 `planned_end_chapter` 或把卷标为 `complete`，`commit` 会在同一事务中运行并保存卷审计，把结果放入 `volume_audit`。出现 `committed_with_audit_blockers` 表示章节已经成功提交，不能重跑同一提交；数据库会拒绝开启序号更高的卷。用当前卷的 `revision` 或 `revalidate` 处理问题会自动生成新审计版本；新审计无 blocker 后才可进入下一卷。

多章日更开始、恢复和批末用 `batch-status` 从数据库推导进度：

```bash
python "<skill-dir>/scripts/story_state.py" batch-status \
  --project "<作品根>" --start <起始章> --end <结束章> --json
```

它不写批次状态，只报告已接受的连续前缀、下一章、剩余章和 `verify` blocker，因此不会形成第二事实源。完整编排见 [daily-batch.md](daily-batch.md)。

## 选择性上下文

写下一章前运行 `context`。可以不传 ID：工具会从近章 `touches` 和未关闭线索推导热集（实体/线索各最多 8 条），并带上相交的时间线与信息边界。自动热集是下限不是全集；冷角色、冷地点继续用 `--entity` / `--thread` 追加。

```bash
python "<skill-dir>/scripts/story_state.py" context \
  --project "<作品根>" --recent 3 --json
python "<skill-dir>/scripts/story_state.py" context \
  --project "<作品根>" --entity char.lin-zhou --thread F001 --recent 3 --json
```

`commit` 成功后会刷新只读派生视图：`追踪/视图/上下文.md`、`追踪/视图/时间线-作者真相.md`、`追踪/视图/时间线-读者已知.md`。它们不能回写数据库；手改后 `verify` 报 `derived-view-drift`。需要重生成时再跑 `context --render-views`。不要把整个数据库导出进上下文。

## 修改旧章

修改前先查询影响：

```bash
python "<skill-dir>/scripts/story_state.py" impact \
  --project "<作品根>" --chapter ch-012 --json
```

- 纯措辞修改且事实、人物弧、线索、时间线和依赖均不变：使用 `mode=revision`、`continuity_changed=false`，不得携带状态变化；仍须重交该版本完整的 `references` 与 `arc_beats`。
- 连续性发生改变：使用 `mode=revision`、`continuity_changed=true`。提交后所有下游章节会标记 `needs_review`，`verify` 阻断，且不能继续 append。
- 按章节顺序复核受影响章节。内容需要改动时提交 `revision`；确认仍成立时提交 `revalidate`，不得携带状态变化，但必须重交确认后的完整依赖与人物弧。所有 `needs_review` 清零后才能续写新章。

依赖图由变更字段、显式 references 和历史写入自动建立。没有提交 reference 的隐含依赖无法被工具发现，因此变更单提取质量是稳定性的关键。

## 回滚和恢复

只允许回滚当前 head：

```bash
python "<skill-dir>/scripts/story_state.py" rollback \
  --project "<作品根>" --commit <commit-id> --json
```

需要让磁盘工作副本回到回滚后的已接受版本时，再显式执行：

```bash
python "<skill-dir>/scripts/story_state.py" checkout \
  --project "<作品根>" --chapter ch-012 --json
```

非 head 历史章不能用 rollback 硬拆提交链，必须走“影响分析 -> revision -> 下游复核”。

## 卷级审计

即使卷终提交已自动运行，也应在交付卷稿前显式读取持久化审计：

```bash
python "<skill-dir>/scripts/story_state.py" audit-volume \
  --project "<作品根>" --volume vol-01 --stale-after 20 --json
```

默认返回该卷最新的已存审计。修订卷内章节时会自动产生新版本；需要在不提交章节的情况下显式重审，可追加 `--refresh`。查看旧版本时使用 `--audit-id <id>`，不能修改或覆盖该结果。

审计内容：

- 核心人物本卷是否有已记录的人物弧变化。
- 到期未处理、长期无推进的伏笔、承诺、问题和剧情债务。
- 待复核章节、无效引用、同刻双地点和正叙时间倒退。
- 前三章与后三章的句长、段长、节奏离散度、对话比例和套式提示密度漂移。

每次审计都绑定 `audit.id`、`end_commit_id` 和 `state_revision`，结果不可覆盖。审计按卷末提交边界重放状态，不用未来全局状态改写旧卷；因此后续卷回收某伏笔，也不会把早先“未按期兑现”的历史结论改成通过。需要改变结论时先修订目标卷章节，提交会保存新的审计版本，并保留可由 `--audit-id` 读取的旧结果及其解决记录。

文风漂移只是定位信号。必须回到正文和 Style Contract 判断，不为追平数值机械拆句、加对话或删除角色特有表达。
