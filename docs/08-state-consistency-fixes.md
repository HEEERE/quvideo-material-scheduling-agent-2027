# 状态一致性与批次排期修复

本次直接检查代码，并先通过新增测试复现缺陷，再修改实现。原有 95 项测试通过；新增 17 项业务回归和 2 项评估路径回归，最终 114 项全部通过。

## 已修复问题

1. **同批待确认建议重叠。** 原查询只检查 READY 草稿，M01、M07 可以同时建议 19:00，甚至并发保存两份互相冲突的建议。现在待确认草稿在同一请求内参与冲突检查；模型需查询不同的候选时段。独立请求仍不会被其他请求的待确认建议占位，硬性时间替代仍必须 PENDING。
2. **历史保存结果覆盖当前审核风险。** 原最终结果优先采用 SAVED，即使重新读取后审核已失效或变为 BLOCKED，也能输出 DRAFT_SAVED。现在当前缺失、阻断、待审核或审核错误优先决定素材处理状态，不再输出可用建议时间；历史草稿留存，保存事实和当前审核分别展示。
3. **失效素材与审核缓存仍被使用。** 原 NOT_FOUND 未清除已加载素材，恢复读取后又未清除 NOT_FOUND；证据工具异常也保留旧 PASS。现在缺失和异常都会使相关缓存失效，必须重新获得证据并审核。测试同时验证恢复后重试仍返回同一草稿，不新增重复记录。
4. **保存失败原因在最终结果中丢失。** 原保存前置拒绝没有替换旧保存结果，最终结果也没有具体错误字段。现在每次已识别素材的保存调用都会记录结果，并输出 save_error；证据读取或规则检查错误输出 review_error。
5. **相对输出目录导致验收误报或崩溃。** 评估器原先对相对路径直接调用 relative_to(ROOT)，且子进程工作目录不同会改变路径含义。现在启动子进程前解析为绝对目录，仓库内证据使用相对链接，仓库外证据保留绝对链接。两个 CLI 均覆盖从其他工作目录传入相对路径的回归测试。

## 验证证据

- [完整测试输出：114 passed](../examples/state-consistency-test-results.txt)。
- 该修复阶段六场景离线验收为 6/6，本地记录在 `runs/state-consistency-evidence/mock_20261101T144555Z_c002cb0f/evaluation.json`。
- 该修复阶段动态离线复验为 2/2，本地记录在 `runs/state-consistency-evidence/review_mock_20261101T144551Z_45097413/regressions.json`。
- 路径问题修复前的失败批次保留在本地 `runs/state-consistency-evidence/mock_20261101T144340Z_0a95d9d4/evaluation.json`，未改写为成功。
- 本轮重新执行的真实模型与离线证据已放入可交付目录，见 [最新评估](03-evaluation.md)、[模拟运营测试](09-business-simulation.md) 和 [最新证据索引](latest-evidence.json)。

完整测试实际命令：

```powershell
.\.venv\Scripts\python.exe -m pytest -q --basetemp runs/pytest-final-20261101-b -o cache_dir=runs/pytest-cache-20261101 --tb=short
```

本机默认 pytest 临时目录和缓存存在权限问题，因此指定了项目内测试目录。复跑时请为 --basetemp 使用新的专用测试目录，pytest 会清理所指定目录。离线验收命令：

```powershell
.\.venv\Scripts\python.exe -m src.evaluation --mode mock --output-dir runs/state-consistency-evidence
.\.venv\Scripts\python.exe -m src.review_evaluation --mode mock --output-dir runs/state-consistency-evidence
```

runs 下的数据库与该阶段验收文件为本地证据，不纳入 Git；测试代码、测试输出和修复说明随源码交付。该修复阶段仅进行了离线验证；之后按用户要求在本轮重新执行真实模型验证及模拟运营链路，并另存于 examples/evidence，旧 Trace 保留为对应版本记录。系统仍只保存草稿。
