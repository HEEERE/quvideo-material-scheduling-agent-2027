# 素材审核与发布排期 Agent

小影科技 2027 届 AI-Agent 岗位笔试项目：面向全球视频内容运营的**真实模型 Tool Calling Agent**。读取合成素材、执行可解释审核、动态查询冲突时段，并真实保存 SQLite 草稿。

**仅生成草稿，不发布内容。所有素材、排期和规则均为演示用合成数据，不代表小影内部系统或平台官方政策。**

## 评审快速入口

| 交付项 | 可核查结果 |
|---|---|
| 工程测试 | [66 passed](examples/test-results.txt) |
| 六场景真实模型验证 | [6/6，DeepSeek deepseek-flash](examples/evidence/live_20261101T133812Z_5b3ae78b/evaluation.json) |
| 离线验证 | [6/6，明确标为 Mock](examples/evidence/mock_20261101T133805Z_4ac28914/evaluation.json) |
| 真实正常路径 | [原始 Trace](examples/evidence/live_20261101T133812Z_5b3ae78b/run_6cafb61e09634eb88a089de23e05c79d/trace.json) |
| 真实冲突路径 | [原始 Trace](examples/evidence/live_20261101T133812Z_5b3ae78b/run_38a1a0aea76a4def9111653a15e1867d/trace.json) |
| 真实错误与恢复 | [模型错误确认状态被 Guard 拒绝并改正](examples/evidence/live_20261101T133154Z_18c0964f/evaluation.json) |
| 逐项验收与限制 | [评估矩阵](docs/03-evaluation.md) |

## 安装

要求 Python 3.11+。在项目根目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

macOS/Linux 使用 `source .venv/bin/activate`。也可直接使用虚拟环境内的 Python，无需激活。直接依赖已固定版本，`requirements-lock.txt` 是本机成功运行环境的完整依赖快照。

## 真实模型运行

把 `.env.example` 复制成本地 `.env` 并填写凭据。OpenAI-compatible Chat Completions 的 Base URL 应包含 API 前缀（例如 `/v1`），程序只追加 `/chat/completions`。

```dotenv
LLM_API_KEY=<在本机填写>
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-flash
LLM_TIMEOUT_SECONDS=45
LLM_EXTRA_BODY={"thinking":{"type":"disabled"}}
```

```powershell
python -m src.main --mode live --input examples/normal.input.json --db runs/live-normal.db
python -m src.main --mode live --input examples/conflict.input.json --db runs/live-conflict.db
```

上述模型是本项目实际验证使用的用户指定模型。更换提供方时须选支持 Chat Completions 原生工具调用的模型，按提供方要求调整额外配置。仅支持其他 API 的模型不在当前适配范围内。未配置、无额度、超时或协议不兼容都会明确失败，不会回退 Mock。

## 离线与测试

```powershell
python -m src.main --mode mock --input examples/normal.input.json --db runs/mock-normal.db
python -m src.main --mode mock --input examples/conflict.input.json --db runs/mock-conflict.db
python -m pytest -q
```

Mock 是固定逻辑模拟器，只验证业务与工程链路。与真实模型使用相同的工具、Guard 和数据库，但不能用它证明自主决策。

## 重跑六场景并保留证据

```powershell
python -m src.evaluation --mode mock
python -m src.evaluation --mode live
```

评估器为每个案例建立独立数据库，避免跨案例占位；duplicate 在两个独立 Python 进程中复用数据库。每次生成新的批次目录，不覆盖旧事件。保存异常案例会显式注入提交前故障。单独体验该案例：

```powershell
python -m src.main --mode mock --input examples/save_failure.input.json --db runs/failure.db --simulate-save-failure
```

若手动用默认共享数据库运行不同请求，相同渠道市场时间可能被之前的 READY 草稿占用，这是业务规则生效。重复运行同一原始请求返回 ALREADY_EXISTS；新 request_id 是独立需求。

## 如何读结果

CLI 输出 JSON 包括审核证据、原时间、建议时间、待确认项、真实保存结果、执行状态、实际 token usage 和 Trace 文件路径。每次运行有 UUID run_id，生成 `events.jsonl`、`trace.json`、`result.json`；最终批次还记录代码与数据哈希。

审核状态为 PASS / NEEDS_INFO / NEEDS_REVIEW / BLOCKED。保存工具为 SAVED / ALREADY_EXISTS / SAVE_FAILED / REJECTED_BY_POLICY。草稿 READY 与 AWAITING_CONFIRMATION 均未发布；硬性时间的替代必须待用户确认。相同渠道市场、开始时间相距严格小于 60 分钟发生冲突；待确认建议不占位，READY 草稿占位。

COMPLETED 表示所有素材得到明确处理结果，可能包含阻断或保存失败。退出码：0 已处理、2 配置或运行失败、3 未完成或预算耗尽、4 保存失败。`--max-steps`、`--max-tool-calls`、`--timeout` 可限制执行；工具非法参数也计入预算。

显式确认通过原始输入的 user_confirmations 提交，模型不能自行添加。精确确认会写入草稿证据，并在保存前重新检查冲突。字段说明及信任边界见 [技术方案](docs/02-solution.md)。

## 文件与文档

- `src/agent.py`、`llm.py`：实际模型决策循环及原生协议。
- `tools.py`、`rules.py`：工具注册、素材补查与确定性审核。
- `guard.py`、`scheduler.py`、`storage.py`：确认边界、冲突校验与 SQLite 事务幂等。
- `tracing.py`、`evaluation.py`：观测、脱敏、来源快照和实际验收。
- [需求分析](docs/01-requirements.md)、[技术方案](docs/02-solution.md)、[评估报告](docs/03-evaluation.md)、[AI 使用记录](docs/04-ai-usage.md)、[真实复盘](docs/05-retrospective.md)。
- [开发时间记录](docs/development-record.json)、[GitHub 交付说明](docs/06-delivery.md)。

限制：当前是单机、合成元数据审核；没有视觉审核、真实许可查询、用户身份认证或生产发布。真实样本数量有限，案例通过不代表生产可靠性。CI 配置随仓库提供，实际远程执行状态以 GitHub 为准。

本地交付核查：`python scripts/verify_delivery.py`，扫描已暂存或跟踪文件的凭据及证据完整性。密钥、本地数据库、虚拟环境、缓存和运行目录都被 `.gitignore` 排除。
