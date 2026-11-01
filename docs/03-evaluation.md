# 实际评估记录

本文件由 `scripts/build_report.py` 从原始评估 JSON 和 pytest 输出生成，未修改 Trace。最后一批真实验证：2026-11-01T13:38:12.339333+00:00 至 2026-11-01T13:39:27.489325+00:00。

## 证据分层

| 类型 | 实际结果 | 证据 |
|---|---|---|
| 确定性 pytest | 66 项通过，耗时 2.26 秒 | [原始输出](../examples/test-results.txt) |
| DeepSeek 真实模型 | 6/6 个场景通过本批次断言 | [完整报告](../examples/evidence/live_20261101T133812Z_5b3ae78b/evaluation.json) |
| 离线脚本模拟 | 6/6 个场景通过 | [独立报告](../examples/evidence/mock_20261101T133805Z_4ac28914/evaluation.json) |

这些是有限案例的实际结果，不代表总体成功率。live 由原生模型工具调用产生；mock 和测试替身不能证明模型自主决策。

最终运行来源指纹：`4fa41e1a11454b4590c2f8fad0c985eb77e684a91f2545fa8ab203f4182955d8`。每个 run 的首条 source_snapshot 记录 src 和 data 文件的 SHA-256。首次探索记录早于此功能加入，仍保留原样。

## 六场景逐项验收

| 场景 | 实际输入 | 预期 | 实际输出 | 断言 | 原始简洁 Trace |
|---|---|---|---|---|---|
| normal | [examples/normal.input.json](../examples/normal.input.json) | PASS，原时间 READY | DRAFT_SAVED / SAVED / READY | 通过 | [Trace](../examples/evidence/live_20261101T133812Z_5b3ae78b/run_6cafb61e09634eb88a089de23e05c79d/trace.json) |
| missing | [examples/missing.input.json](../examples/missing.input.json) | 补查仍缺授权，不保存 | NEEDS_INFO / NOT_ATTEMPTED | 通过 | [Trace](../examples/evidence/live_20261101T133812Z_5b3ae78b/run_180b08492e394a028007a66bd8623b80/trace.json) |
| risk | [examples/risk.input.json](../examples/risk.input.json) | 明确禁止，BLOCKED | BLOCKED / NOT_ATTEMPTED | 通过 | [Trace](../examples/evidence/live_20261101T133812Z_5b3ae78b/run_9783558da43d4735bddb9192402ac833/trace.json) |
| conflict | [examples/conflict.input.json](../examples/conflict.input.json) | 主动另查，硬性替代 PENDING | DRAFT_SAVED / SAVED / AWAITING_CONFIRMATION | 通过 | [Trace](../examples/evidence/live_20261101T133812Z_5b3ae78b/run_38a1a0aea76a4def9111653a15e1867d/trace.json) |
| duplicate | [examples/duplicate.input.json](../examples/duplicate.input.json) | 跨进程同 ID，仅一行 | DRAFT_SAVED / ALREADY_EXISTS / READY | 通过 | [Trace](../examples/evidence/live_20261101T133812Z_5b3ae78b/run_de15a6f68de046c7a446eb1d32e28ab0/trace.json) |
| save_failure | [examples/save_failure.input.json](../examples/save_failure.input.json) | 回滚，明确未保存 | SAVE_FAILED / SAVE_FAILED | 通过 | [Trace](../examples/evidence/live_20261101T133812Z_5b3ae78b/run_ec615ae8e03e4f45b36f5f8886d21e4e/trace.json) |

每份 evaluation.json 记录输入路径、实际退出码、完整结果、持久化数据库快照、工具路径、Trace 链接；duplicate 包含两个独立进程的结果，第二次使用同一数据库。数据库文件作为运行缓存被 Git 忽略，快照和事务测试供复核。save_failure 的退出码 4 是明确失败处理的预期结果，COMPLETED 只表示场景已处理完，不表示成功保存。

## 两条实际动态路径

正常路径：[Trace](../examples/evidence/live_20261101T133812Z_5b3ae78b/run_6cafb61e09634eb88a089de23e05c79d/trace.json)，实际工具序列：`read_material → check_review_rules → query_schedule → save_schedule_draft`。

冲突路径：[Trace](../examples/evidence/live_20261101T133812Z_5b3ae78b/run_38a1a0aea76a4def9111653a15e1867d/trace.json)，实际工具序列：`read_material → check_review_rules → query_schedule → query_schedule → save_schedule_draft`。同一个运行时和相同工具集合，工具观察导致后续查询动作发生变化。候选时间由模型选择，程序仅返回占用事实、用户窗口和间隔规则。

## 真实发现与修复

首轮真实冲突验证中模型错误选择 WITHIN_FLEXIBILITY，Guard 拒绝后模型改用 PENDING，实际保存为待确认建议。未篡改原日志，详见 [首轮报告](../examples/evidence/live_20261101T133154Z_18c0964f/evaluation.json)。后续完整批次仍有不同数量的时段查询，显示模型路径并非固定脚本。

保存异常测试在 INSERT 后注入真实 SQLite 异常，验证 rollback。真实模型有自主重试的记录，所有失败都返回 SAVE_FAILED，没有伪成功或额外行。

实现检查后补充了空白元数据、预检错误记录、数据库成功复查失败和并发不同请求竞争时段的保护与测试。最终复核曾因切换沙箱身份无法访问既有临时目录而出现环境错误，实际记录见 [失败摘要](../examples/test-results-permission-error.txt)。换用新的项目内临时目录复测后，全部 66 项通过；没有删除案例或放宽规则。

本机实际复测命令：

```powershell
.\.venv\Scripts\python -m pytest -q --basetemp runs/pytest-final-20261101-1340 -o cache_dir=runs/pytest-cache-final
```

一般环境直接使用 `python -m pytest -q`。GitHub Actions 已提供 Python 3.11/3.12、Ubuntu/Windows 的测试矩阵配置，远程工作流结果需以 GitHub 实际状态为准，本报告不声称未查看的工作流已通过。

## 限制

只审核合成元数据，不进行视频视觉分析；演示规则不是官方政策。真实模型样本仅覆盖这几个固定业务案例，输出有随机性。保存异常是显式故障注入；确认是本地可信 JSON 输入，没有身份认证。不存在真实发布能力。模型 API 需要可用网络、兼容协议及额度，live 失败时不会回退 mock。过期或取消草稿清理未实现，READY 草稿持续占用时段。
