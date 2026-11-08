---
title: 任务标签规范（部门自管 · 集团标准 + 部门个性双轨）
type: document
permalink: ai-memory/labels-规范
cluster: Common-公共库
zone: common
tags:
- 标签规范
- 任务系统
- 双轨
updated: 2026-11-07
related: [[集群导航-Common-公共库]]  #related_autohub
related_autohub: true
---

# 任务标签规范（部门自管 · 集团标准 + 部门个性双轨）

> **本文件是模板**，每个部门仓根目录放一份（由 `scaffold_cluster.py` / `orca-dept-bootstrap` 落地）。
> 主管照此**自己设计**本部门业务标签，同时守住集团统一底座。
> 校验工具：`check_issue_labels.py`（2026-11-04 升级为双轨——集团标准缺失/不符报错，部门个性放行）。

## 一、两条铁律（改本文件前先读）

1. **集团标准标签的 `name` 字段不准改**：基础集（下表）是全局对齐的，主管只能**加**不准**改**基础集的形态（`英文 中文` 混合，如 `bug 缺陷`）。
2. **部门个性标签必须带 `部门-NN` 前缀**：如 `部门-11 模型微调`、`部门-11 显存爆了`。前缀隔离，不污染集团、不撞车。

## 二、集团统一标签（基础集 · 不准改 name，只准补）

> 真源：`github-issue-task-system` §2.2；本表镜像之。主管新增业务标签时**不要**动这些。

### 通用（14）
`bug 缺陷` · `question 疑问` · `enhancement 增强` · `documentation 文档` · `plan 计划`
`P1 最优先` · `P2 高优先` · `P3 中优先` · `duplicate 重复` · `invalid 无效` · `wontfix 不修` · `help wanted 求援` · `good first issue 新手友好` · `accessibility 无障碍`

### 部门类（按编号口径，一号一部门）
`部门-00 首页` · `部门-01 Digger` · `部门-02 TA` · `部门-03 Network` · `部门-05 游戏` · `部门-06 Tech` · `部门-07 动捕` · `部门-08 颐康宝` · `部门-09 YouPick` · `部门-10 FreeToken` · `部门-11 ImageGen` · `部门-12 Content` · `部门-13 Ecommerce` · `部门-14 Hardware` · `部门-91 Security` · `部门-92 Finance` · `部门-93 Backup` · `部门-94 Support` · `部门-ALL 公共`

### 状态/验收
`状态-进行中` · `状态-待验收` · `状态-已验收` · `验收-老泡儿` · `验收-00_01`

## 三、部门个性标签（主管自管区 · 自己设计、自己加）

> **这是主管的权限**：根据本部门业务需要，往下列加标签，全部带 `部门-NN` 前缀。
> 例（02-TA 金融交易框架部）：
> `部门-02 回测` · `部门-02 数据契约` · `部门-02 风控辩论` · `部门-02 多智能体` · `部门-02 决策框架`

| 部门个性标签（带前缀） | 用途 | 加的人 |
|----------------------|------|--------|
| `部门-02 回测` | 回测相关任务（回测框架、评分锚点、HTML 报告） | 02_00 主管 |
| `部门-02 数据契约` | Point-in-time 数据完整性（look-ahead 防护、决策日期过滤） | 02_00 主管 |
| `部门-02 风控辩论` | 风控三派辩论机制（激进/保守/中立对抗验证） | 02_00 主管 |
| `部门-02 多智能体` | 多智能体编排（analysts/researchers/managers/risk_mgmt/trader） | 02_00 主管 |
| `部门-02 决策框架` | 决策/研究相关（买卖建议、策略评估、LLM 决策） | 02_00 主管 |

## 四、怎么加 / 怎么改（主管实操）

```bash
# 加一个部门个性标签（带前缀）
gh label create "部门-02 回测" --repo goodbj/TradingAgents \
  --description "02-TA 部门：回测相关任务（回测框架、评分锚点、HTML 报告）" --color "BFDADC"

# 改集团标准标签的形态（❌ 不建议，除非 00_01 统一发令）
gh label edit "P1" --name "P1 最优先" --repo goodbj/TradingAgents

# 自检（双轨：集团标准缺失/不符→报错；部门个性→放行）
python -I Common-公共库/02-Toolchain-工具链/scripts/check_issue_labels.py --repo goodbj/TradingAgents
```

## 五、实时修正 / 标准化闭环

1. 主管在**本部门仓**改这份规范（加个性标签段）→ 跑 `gh label create/edit` 落地到 GitHub。
2. 跑 `check_issue_labels.py --repo <本部门仓>` 自检：集团标准不能破，部门个性随便加。
3. 想改**集团标准集**（基础集）→ 这不是部门权限，投公共簇信箱（以 `orca orchestration run-list` 现查「公共簇信箱」为准，当前 `run_1cd4e1700daf`）找 00_01，由 00_01 改 `check_issue_labels.py` 的 STANDARD 后统一生效。

## 六、与任务巡检命令对齐（v2 补 · 避免占位符陷阱）

> 主管/执行层模板里的 `gh issue list --label "部门-<NN> XXX"` 是占位示意，**真实巡检必须填本文件 §二 的枚举值**，例如：

```bash
# 02-TA 主管巡检（真标签值，非占位符）
gh issue list --repo goodbj/TradingAgents --state open --label "部门-02 TA"

# 02-TA 待验收
gh issue list --repo goodbj/TradingAgents --state all --label "状态-待验收" --label "部门-02 TA"
```

- **信道 ↔ 标签对应**：本部门信道 ID 以 `任务分发制度 §5.5 信道速查表` 现役为准；本部门标签以本文件 §二 枚举为准。两处都是真值，模板里的 `<NN>` / `run_...` 一律替换后使用。
- **04 号段空置**：`部门-04` 不出现在基础集（04 已退役、号段空置供新部门），勿自建 `部门-04`。新部门占 04 后由 00_01 补进 STANDARD 并同步本表。

## Relations

- relates_to [[github-issue-task-system]]
- relates_to [[orca-dept-bootstrap]]
- relates_to [[命名规范]]
