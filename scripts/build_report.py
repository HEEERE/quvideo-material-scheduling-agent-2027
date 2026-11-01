"""Build reviewer indexes from actual reports; never mutate raw evidence."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def latest(mode):
    candidates = []
    for path in (ROOT / "examples/evidence").glob("*/evaluation.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        if data["execution_mode"] == mode and data["total"] == 6:
            candidates.append((data["started_at"], path, data))
    _, path, data = max(candidates)
    return path, data


def portable(value):
    return Path(value.replace("\\", "/")).as_posix()


def main():
    live_path, live = latest("live")
    mock_path, mock = latest("mock")
    test_text = (ROOT / "examples/test-results.txt").read_text(encoding="utf-8-sig")
    match = re.search(r"(\d+) passed in ([0-9.]+)s", test_text)
    if not match:
        raise RuntimeError("Latest pytest output is not successful")
    normal, conflict = [next(c for c in live["cases"] if c["case_id"] == name) for name in ("normal", "conflict")]
    ln, lc = portable(normal["trace"]["trace"]), portable(conflict["trace"]["trace"])
    relative_live, relative_mock = live_path.relative_to(ROOT).as_posix(), mock_path.relative_to(ROOT).as_posix()
    first_event = json.loads((ROOT / portable(normal["trace"]["events"])).read_text(encoding="utf-8").splitlines()[0])
    snapshot = first_event.get("source_fingerprint", "unavailable")
    rows = []
    expected = {"normal":"PASS，原时间 READY", "missing":"补查仍缺授权，不保存", "risk":"明确禁止，BLOCKED", "conflict":"主动另查，硬性替代 PENDING", "duplicate":"跨进程同 ID，仅一行", "save_failure":"回滚，明确未保存"}
    for case in live["cases"]:
        out = case["result"]["outcomes"][0]
        draft = out.get("draft") or {}
        actual = out["status"] + " / " + out["save_status"]
        if draft:
            actual += " / " + draft["draft_status"]
        input_path = portable(case["input"])
        rows.append(f"| {case['case_id']} | [{input_path}](../{input_path}) | {expected[case['case_id']]} | {actual} | {'通过' if case['passed'] else '失败'} | [Trace](../{portable(case['trace']['trace'])}) |")
    evaluation = f"""# 实际评估记录

本文件由 `scripts/build_report.py` 从原始评估 JSON 和 pytest 输出生成，未修改 Trace。最后一批真实验证：{live['started_at']} 至 {live['finished_at']}。

## 证据分层

| 类型 | 实际结果 | 证据 |
|---|---|---|
| 确定性 pytest | {match[1]} 项通过，耗时 {match[2]} 秒 | [原始输出](../examples/test-results.txt) |
| DeepSeek 真实模型 | {live['passed']}/{live['total']} 个场景通过本批次断言 | [完整报告](../{relative_live}) |
| 离线脚本模拟 | {mock['passed']}/{mock['total']} 个场景通过 | [独立报告](../{relative_mock}) |

这些是有限案例的实际结果，不代表总体成功率。live 由原生模型工具调用产生；mock 和测试替身不能证明模型自主决策。

最终运行来源指纹：`{snapshot}`。每个 run 的首条 source_snapshot 记录 src 和 data 文件的 SHA-256。首次探索记录早于此功能加入，仍保留原样。

## 六场景逐项验收

| 场景 | 实际输入 | 预期 | 实际输出 | 断言 | 原始简洁 Trace |
|---|---|---|---|---|---|
{chr(10).join(rows)}

每份 evaluation.json 记录输入路径、实际退出码、完整结果、持久化数据库快照、工具路径、Trace 链接；duplicate 包含两个独立进程的结果，第二次使用同一数据库。数据库文件作为运行缓存被 Git 忽略，快照和事务测试供复核。save_failure 的退出码 4 是明确失败处理的预期结果，COMPLETED 只表示场景已处理完，不表示成功保存。

## 两条实际动态路径

正常路径：[Trace](../{ln})，实际工具序列：`{' → '.join(normal['tool_path'])}`。

冲突路径：[Trace](../{lc})，实际工具序列：`{' → '.join(conflict['tool_path'])}`。同一个运行时和相同工具集合，工具观察导致后续查询动作发生变化。候选时间由模型选择，程序仅返回占用事实、用户窗口和间隔规则。

## 真实发现与修复

首轮真实冲突验证中模型错误选择 WITHIN_FLEXIBILITY，Guard 拒绝后模型改用 PENDING，实际保存为待确认建议。未篡改原日志，详见 [首轮报告](../examples/evidence/live_20261101T133154Z_18c0964f/evaluation.json)。后续完整批次仍有不同数量的时段查询，显示模型路径并非固定脚本。

保存异常测试在 INSERT 后注入真实 SQLite 异常，验证 rollback。真实模型有自主重试的记录，所有失败都返回 SAVE_FAILED，没有伪成功或额外行。

实现检查后补充了空白元数据、预检错误记录、数据库成功复查失败和并发不同请求竞争时段的保护与测试。最终复核曾因切换沙箱身份无法访问既有临时目录而出现环境错误，实际记录见 [失败摘要](../examples/test-results-permission-error.txt)。换用新的项目内临时目录复测后，全部 {match[1]} 项通过；没有删除案例或放宽规则。

本机实际复测命令：

```powershell
.\\.venv\\Scripts\\python -m pytest -q --basetemp runs/pytest-final-20261101-1340 -o cache_dir=runs/pytest-cache-final
```

一般环境直接使用 `python -m pytest -q`。GitHub Actions 已提供 Python 3.11/3.12、Ubuntu/Windows 的测试矩阵配置，远程工作流结果需以 GitHub 实际状态为准，本报告不声称未查看的工作流已通过。

## 限制

只审核合成元数据，不进行视频视觉分析；演示规则不是官方政策。真实模型样本仅覆盖这几个固定业务案例，输出有随机性。保存异常是显式故障注入；确认是本地可信 JSON 输入，没有身份认证。不存在真实发布能力。模型 API 需要可用网络、兼容协议及额度，live 失败时不会回退 mock。过期或取消草稿清理未实现，READY 草稿持续占用时段。
"""
    (ROOT / "docs/03-evaluation.md").write_text(evaluation, encoding="utf-8")
    readme = f"""# 素材审核与发布排期 Agent

小影科技 2027 届 AI-Agent 岗位笔试项目：面向全球视频内容运营的**真实模型 Tool Calling Agent**。读取合成素材、执行可解释审核、动态查询冲突时段，并真实保存 SQLite 草稿。

**仅生成草稿，不发布内容。所有素材、排期和规则均为演示用合成数据，不代表小影内部系统或平台官方政策。**

## 评审快速入口

| 交付项 | 可核查结果 |
|---|---|
| 工程测试 | [{match[1]} passed](examples/test-results.txt) |
| 六场景真实模型验证 | [{live['passed']}/{live['total']}，DeepSeek deepseek-flash]({relative_live}) |
| 离线验证 | [{mock['passed']}/{mock['total']}，明确标为 Mock]({relative_mock}) |
| 真实正常路径 | [原始 Trace]({ln}) |
| 真实冲突路径 | [原始 Trace]({lc}) |
| 真实错误与恢复 | [模型错误确认状态被 Guard 拒绝并改正](examples/evidence/live_20261101T133154Z_18c0964f/evaluation.json) |
| 逐项验收与限制 | [评估矩阵](docs/03-evaluation.md) |

## 安装

要求 Python 3.11+。在项目根目录执行：

```powershell
python -m venv .venv
.\\.venv\\Scripts\\Activate.ps1
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
LLM_EXTRA_BODY={{"thinking":{{"type":"disabled"}}}}
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
"""
    (ROOT / "README.md").write_text(readme, encoding="utf-8")
    metadata = {"first_verified_clock_utc": "2026-11-01T13:23:56+00:00", "report_generated_at_utc": datetime.now(timezone.utc).isoformat(),
                "client_timezone": "Asia/Taipei", "measurement": "Wall-clock interval from first tool clock reading to report generation; not human labor time or model-only compute time.",
                "pytest_passed": int(match[1]), "pytest_measured_seconds": float(match[2]),
                "latest_live_report": relative_live, "latest_mock_report": relative_mock,
                "source_fingerprint": snapshot}
    start = datetime.fromisoformat(metadata["first_verified_clock_utc"])
    end = datetime.fromisoformat(metadata["report_generated_at_utc"])
    metadata["verified_elapsed_seconds"] = round((end-start).total_seconds(), 3)
    (ROOT / "docs/development-record.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    main()
