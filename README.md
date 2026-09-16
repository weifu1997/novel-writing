# 小说写作

面向中文小说的 Agent Skill：覆盖扫榜选题、对标拆文、开书立项、长短篇规划与写作、串行日更、旧稿接管、审稿返修，以及长篇连续性管理。质量靠字数门禁、文风契约和可提交的状态库约束，不承诺爆款、真人率，也不把榜单经验当成写作公式。

适用于番茄、七猫、起点等平台方向；不用于非虚构文案。

## 这是什么

本仓库是一套可加载的写作技能，而不是现成的小说成品。Agent 按当前任务只读取相关资料，把用户意图、已确认项目事实和文风契约落实成可用产物：大纲、章纲、正文、审稿意见或连续性记录。

工作原则：

- 先保证故事成立、人物可信、字数合约准确，再考虑平台偏好。
- 规划、正文、接管、审稿和返修是不同授权，不自动串联。
- 当前请求优先于已确认项目事实，再是本书文风契约、平台 profile 和通用建议。

完整行为约定见 [SKILL.md](SKILL.md)，工作流见 [references/workflow.md](references/workflow.md)。

## 目录

```text
SKILL.md                 技能入口与交付门禁
agents/openai.yaml       对外展示名称与默认提示
references/              按任务加载的写作资料
  workflow.md            模式路由：立项、规划、写章、日更、接管、审稿
  genre/                 题材工艺（都市、悬疑、言情、历史、奇幻科幻等）
  chapter-craft/         章法卡（开篇、信息悬念、对话关系、行动、调查、高潮、过渡）
  platform-profiles.md   平台写作侧假设，不是当期规则原文
scripts/                 确定性门禁与状态工具
tests/                   脚本与状态库测试
```

`references/` 按任务精确加载，不要一次性塞进上下文。题材模块决定冲突材料，章法模块决定组织方式，两者不能互相替代。

## 常用脚本

默认字数口径是 `visible_chars_v1`：去掉可识别 YAML frontmatter、首个章节标题和 Markdown 装饰后，统计正文中所有非空白可见字符，标点计入。需要纯汉字数时加 `--metric han_chars`。

```bash
# 单章字数门禁
python scripts/chapter_guard.py 正文/第1章.md --min 2500 --max 3000
python scripts/chapter_guard.py 正文/第1章.md --target 3000 --tolerance 0.05

# 短篇按整稿验收
python scripts/manuscript_guard.py 短篇/01.md 短篇/02.md --min 6000 --max 8000

# 文风启发式检查（不是 AI 概率）
python scripts/prose_lint.py 正文/第1章.md

# 公开榜单样本规范化后再验收
python scripts/market_fetch.py --qidian-mobile hotsales --json
python scripts/market_sample.py 样本.json

# 旧稿只盘点、不改原文
python scripts/legacy_inventory.py 旧稿目录/

# 可续跑的拆文工作区
python scripts/analysis_workspace.py init --workspace 拆文库/书名 --source 原文.txt --title 书名 --kind long
```

百万字或需要强连续性的项目，以 `追踪/story-state.sqlite3` 为已接受事实源：

```bash
python scripts/story_state.py init --project 项目目录 --title 书名
python scripts/story_state.py check --project 项目目录 --body 正文/第1章.md --record 追踪/逐章记录/第1章.md --delta 变更单.json
python scripts/story_state.py commit --project 项目目录 --body 正文/第1章.md --record 追踪/逐章记录/第1章.md --delta 变更单.json
python scripts/story_state.py context --project 项目目录
python scripts/story_state.py impact --project 项目目录 --chapter ch-001
python scripts/story_state.py verify --project 项目目录
```

`check` 和 `commit` 都会对当时正文重新计数。提交失败时，正文快照、章节记录和全局状态都不得推进。修改旧章前先跑 `impact`；下游章节复核完成、`verify` 清零 `needs_review` 之前不要续写。

## 测试

```bash
python -m unittest tests/test_story_state.py tests/test_tools.py
```

## 使用边界

- 平台规则、收益和算法数字会过期，必须查官方当期来源；本地 profile 只提供写作侧假设。
- `prose_lint.py` 只定位可能的套式句、解释腔、库存动作和节奏过齐，命中项要回到语境判断，不能机械改写。
- 状态库保证流程可验证、冲突可阻断、提交可回滚，不保证文学作品绝对零错误。
