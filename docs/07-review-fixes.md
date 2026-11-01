# 审核问题修复与复验

本次修复对应审核提交 `dee9eecbcae052164d444506d4be0a109414bb35` 的三项确定问题，以及排期 JSON 与数据库的同步契约。未修改历史 Trace，所有复验使用新批次目录。

## 1. 占用变化后的重复提交

此前同指纹草稿直接 ALREADY_EXISTS，绕过当前冲突复查。现在读取已有记录后，在同一 BEGIN IMMEDIATE 事务内查询它的实际时段并校验 Guard：

- 当前仍有效：返回同一 ID 与原内容，不重复生成草稿。
- 待确认建议失效：模型查询新的合法时间后，可以重排同一草稿；返回 SAVED / REPLANNED，revision 增加，旧版本持久化到 draft_revisions。
- 已批准草稿失效：明确 REJECTED_BY_POLICY / EXISTING_DRAFT_INVALID，不自动更改批准含义。

重排仍保护原硬性时间、用户窗口和确认状态；未经明确确认的替代仍为 PENDING / AWAITING_CONFIRMATION。最终输出重新查询实际保存时间，失效时输出冲突和待办，不把失效时间继续作为当前建议。

覆盖了同 ID 重排、当前有效时不改写、非法确认状态、窗口越界、并发只重排一次、重排事务回滚、旧版本保留，以及提交后出现冲突等测试。

## 2. 宣传声明类型校验

review_metadata 改为带有严格布尔字段的 ReviewMetadata，保留其余来源信息。字符串 "false"、0、1、列表等输入被 Pydantic 拒绝。已声明的宣传内容若核实状态为 false、null 或缺失，进入 NEEDS_REVIEW；没有声明与声明未核实分开表示，序列化往返不会改变含义。

## 3. 确认请求进入最终用户结果

工具层保存真实时段观察。硬性时间冲突且没有授权调整窗口时，最终输出 NEEDS_CONFIRMATION、冲突依据、所需 flexible_time_window / user_confirmations 和可执行待办。模型自然语言不是批准证据。无变化的补查不再清空已完成的审核结果；真实变化仍要求重新检查规则。

## 4. 正式排期数据契约

初始化种子不会在重启时覆盖现有数据库。新增显式同步入口：

```powershell
python -m src.main --sync-schedules data/schedules.json --db runs/drafts.db
```

按 schedule_id 更新记录的时间和状态，支持 CANCELLED；省略记录保留。整批校验失败不更新任何排期。不调用模型，单独保存维护操作证据。随后使用同一个数据库运行 Agent 即可验证新的占用状态。

## 实际测试与真实复验

最初新增 25 项回归检查，在原实现上得到 23 failed / 2 passed，原始输出保存在 [修复前记录](../examples/review-regressions-before.txt)。完成三项修复后与原有测试合计 91 项通过。

首轮真实动态复验中，重排场景的初次运行未生成草稿：模型查询 19:05、20:05、20:55，却漏掉恰好可用的 19:00 和 21:00。因此该批次如实保留为 1/2，通过的只有无窗口确认场景：[失败批次](../examples/evidence/review_live_20261101T141308Z_41037829/regressions.json)。对应运行文件保留初始未完成的完整观察，没有伪造初始草稿来推进后续场景。

随后查询工具增加了根据真实冲突间距和窗口端点计算的 UNVERIFIED 边界提示，并明确要求模型自己继续查询。没有增加自动查询或自动模型重试。新增两个测试验证提示跟随占用数据变化，并且不代表可用或已选择。当前工程测试共 93 项，实际结果见 [测试输出](../examples/test-results.txt) 和 [实际命令](../examples/test-session.json)。

最终重新运行六个基础场景和两个审核动态边界；以 [最新评估报告](03-evaluation.md) 与 [最新证据索引](latest-evidence.json) 中记录的实际结果为准。真实与离线结果分别保留。重跑命令：

```powershell
python -m src.evaluation --mode live
python -m src.review_evaluation --mode live
python -m src.evaluation --mode mock
python -m src.review_evaluation --mode mock
```

审核动态复验分别验证：同请求第一次保存建议后，通过正式同步命令占用该建议时间，再独立进程提交并检查同 ID、单行、revision=2、历史及当前空闲；无窗口的硬性冲突则检查最终待确认项、冲突证据、审核结果和零草稿。

## 限制

已批准草稿失效时需用户明确处理，不提供自动改期批准或生产发布。查询与最终校验只反映当时状态。元数据、排期与审核规则依然为合成演示数据，模型输出仍有随机性，本次样本不能推导总体成功率。
