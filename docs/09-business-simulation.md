# 模拟运营流程与真实模型测试

本轮测试在当前状态一致性更新上实际执行。素材和用户操作是合成测试输入；live 中的工具选择由真实 DeepSeek deepseek-flash 产生，SQLite 查询、同步、提交和回滚都真实执行。没有调用真实内容发布接口，也没有把模拟确认当作真实运营批准。

模拟运营批次时间（Asia/Taipei）：2026-11-01T22:55:50.489100+08:00 至 2026-11-01T22:58:09.559525+08:00。

| 验证层次 | 真实模型结果 | 离线对照 |
|---|---|---|
| 六项强制场景 | [6/6](../examples/evidence/live_20261101T145550Z_831feb5a/evaluation.json) | [6/6](../examples/evidence/mock_20261101T145551Z_d7b2838e/evaluation.json) |
| 审核动态边界 | [2/2](../examples/evidence/review_live_20261101T145545Z_4e957c40/regressions.json) | [2/2](../examples/evidence/review_mock_20261101T145559Z_eb29467b/regressions.json) |
| 模拟运营链路 | [5/5](../examples/evidence/business_live_20261101T145550Z_26cae819/business.json) | [5/5](../examples/evidence/business_mock_20261101T145605Z_4bf2e16e/business.json) |
| 确定性工程测试 | 114 项通过，15.15 秒 | [原始结果](../examples/test-results.txt) |

这是有限场景的实际断言结果，不是总体成功率。六项场景覆盖正常、授权缺失、版权阻断、冲突、跨进程重复提交与保存失败；后者使用明确标注的真实事务提交前故障注入。审核证据源异常、缓存失效与并发竞争由受控工程测试验证，不声称接入了真实外部素材系统。

## 五个模拟运营场景

| 操作 | 本批实际断言 | 证据 |
|---|---|---|
| 待确认 → 显式确认 → 重复提交 | 通过 | [实际 Trace](../examples/evidence/business_live_20261101T145550Z_26cae819/run_4d270939ea7d46ba86c2123e710b9825/trace.json) |
| 确认后时段被占用 | 通过 | [实际 Trace](../examples/evidence/business_live_20261101T145550Z_26cae819/run_87a2c05894884a489489285cb1e8b2a7/trace.json) |
| 同请求两素材建议避让 | 通过 | [实际 Trace](../examples/evidence/business_live_20261101T145550Z_26cae819/run_de38f562baf74553bfb41e5dc91395d2/trace.json) |
| 日本市场与东京时区 | 通过 | [实际 Trace](../examples/evidence/business_live_20261101T145550Z_26cae819/run_e39b40069906409681d52cb3b8ee8dd1/trace.json) |
| 未核实声明转人工 | 通过 | [实际 Trace](../examples/evidence/business_live_20261101T145550Z_26cae819/run_677e7bb1a535449989563c46925b4b08/trace.json) |

确认链路先产生 PENDING 建议，再由模拟测试操作员在原始输入中填写精确 user_confirmations，模型据此保存 USER_CONFIRMED / READY 草稿。批准后重复提交相同输入，须返回该批准草稿的同一 ID。原待确认请求与新增确认输入的指纹不同，因此会保留两条不同请求状态的记录；重复批准输入不能增加第三条。

确认后占用变化的场景先构造模拟确认输入，再显式同步一条占用该时间的正式排期。断言数据库不存在该时间的 READY 草稿；模型只能提出另一个 PENDING 建议或明确请求进一步确认，不能把原确认移用到新时间。

批量场景在同一请求中处理 M01 和可补查授权的 M07，两者的待确认建议必须至少相隔 60 分钟，并保持未批准。日本场景核对 JP、日语素材、Instagram Reels 和 Asia/Tokyo；声明场景要求人工复核且零草稿。

每个案例都保存实际输入、各阶段退出码和最终 JSON、必要原始事件、简洁 Trace 及 SQLite 快照。确认及重复提交在独立 Python 进程中执行，共享该案例数据库，不依赖模型记忆。不同案例使用独立数据库以避免相互占位。Agent 预算在这些运营场景中显式设置为 32 步、48 次工具调用，以支持多素材处理。

## 测试脚本首次误判

首次离线运营批次为 4/5：[原始报告](../examples/evidence/business_mock_20261101T145433Z_667f161e/business.json)。确认已真实保存，重复提交也复用了 ID；失败来自脚本把等价的 UTC `Z` 与 `+00:00` 按字符串比较。随后改为用 UserConfirmation 校验并比较相同时间值，material_id、目标时间、确认时间和来源仍全部要求一致。没有放宽审批规则，也没有改写失败报告。

## 复现命令

```powershell
python -m src.evaluation --mode live
python -m src.review_evaluation --mode live
python scripts/business_simulation.py --mode live
python scripts/business_simulation.py --mode mock
python -m pytest -q
```

工程测试的本机实际命令见 [test-session.json](../examples/test-session.json)。最新运行时来源指纹为 `1ef21d598b33eb22e38ed1251ec8415c94caa57c3aecbbeff38dd15a769f6ee2`，交付检查同时核对真实与离线批次的每个来源快照；历史记录保留为对应版本证据。用户要求上传的状态一致性与路径更新已一并复验，见 [修复说明](08-state-consistency-fixes.md)。
