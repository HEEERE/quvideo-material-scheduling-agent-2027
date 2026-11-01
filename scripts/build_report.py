"""Build reviewer indexes from actual reports; never mutate raw evidence."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

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


def latest_regressions(mode):
    candidates = []
    for path in (ROOT / "examples/evidence").glob("review_*/regressions.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        if data["execution_mode"] == mode:
            candidates.append((data["started_at"], path, data))
    _, path, data = max(candidates)
    return path, data


def latest_business(mode):
    candidates = []
    for path in (ROOT / "examples/evidence").glob("business_*/business.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        if data["execution_mode"] == mode and data["total"] == 5:
            candidates.append((data["started_at"], path, data))
    _, path, data = max(candidates)
    return path, data


def main():
    live_path, live = latest("live")
    mock_path, mock = latest("mock")
    review_path, review = latest_regressions("live")
    mock_review_path, mock_review = latest_regressions("mock")
    business_path, business = latest_business("live")
    mock_business_path, mock_business = latest_business("mock")
    business_link = business_path.relative_to(ROOT).as_posix()
    mock_business_link = mock_business_path.relative_to(ROOT).as_posix()
    review_link = review_path.relative_to(ROOT).as_posix()
    mock_review_link = mock_review_path.relative_to(ROOT).as_posix()
    test_session = json.loads((ROOT / "examples/test-session.json").read_text(encoding="utf-8"))
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
| 审核发现的动态边界，真实模型 | {review['passed']}/{review['total']} 个场景通过 | [真实修复复验](../{review_link}) |
| 审核发现的动态边界，离线 | {mock_review['passed']}/{mock_review['total']} 个场景通过 | [离线修复复验](../{mock_review_link}) |
| 模拟运营流程，真实模型 | {business['passed']}/{business['total']} 个场景通过 | [完整操作记录](../{business_link}) |
| 模拟运营流程，离线 | {mock_business['passed']}/{mock_business['total']} 个场景通过 | [独立对照](../{mock_business_link}) |

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

审核后新增待确认建议失效重排、同 ID 历史版本、并发重排、重排回滚、声明字段严格类型、确认请求输出和显式排期同步等回归测试。新增 25 项检查在修复前是 23 failed / 2 passed，失败输出见 [修复前记录](../examples/review-regressions-before.txt)；修复后与原有测试共同达到 {match[1]} 项通过。真实复验分别检查“占用变化后同请求保留 ID 重排”和“无窗口硬性冲突输出待确认项”，详见 [修复说明](07-review-fixes.md)。

本轮同时核验工作区已有的状态一致性与评估路径更新：同请求的待确认建议互相避让，失效审核不能被历史保存成功覆盖，保存与证据错误明确输出。完整 {match[1]} 项工程测试重新执行，见 [状态一致性说明](08-state-consistency-fixes.md)。运营模拟使用合成数据和模拟用户操作，Agent 动作来自真实 DeepSeek；确认记录仅为测试输入，详见 [业务模拟报告](09-business-simulation.md)。

初次开发复核曾因切换沙箱身份无法访问既有临时目录而出现环境错误，实际记录见 [失败摘要](../examples/test-results-permission-error.txt)。使用项目内独立临时目录后复测通过；没有删除案例或放宽规则。

本机实际复测命令：

```powershell
{test_session['command']}
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

当前状态一致性更新已用本轮真实模型和离线两类记录复验，覆盖同批建议冲突、当前审核状态、保存错误与评估目录解析。另增加模拟运营人员确认、确认后占用变化、多素材及本地化操作，见 [状态一致性修复](docs/08-state-consistency-fixes.md) 和 [业务模拟报告](docs/09-business-simulation.md)。

| 交付项 | 可核查结果 |
|---|---|
| 工程测试 | [{match[1]} passed](examples/test-results.txt) |
| 六场景真实模型验证 | [{live['passed']}/{live['total']}，DeepSeek deepseek-flash]({relative_live}) |
| 离线验证 | [{mock['passed']}/{mock['total']}，明确标为 Mock]({relative_mock}) |
| 真实正常路径 | [原始 Trace]({ln}) |
| 真实冲突路径 | [原始 Trace]({lc}) |
| 真实错误与恢复 | [模型错误确认状态被 Guard 拒绝并改正](examples/evidence/live_20261101T133154Z_18c0964f/evaluation.json) |
| 逐项验收与限制 | [评估矩阵](docs/03-evaluation.md) |
| 本次审核问题修复 | [真实模型 {review['passed']}/{review['total']}]({review_link})、[修复说明](docs/07-review-fixes.md) |
| 模拟运营流程 | [真实模型 {business['passed']}/{business['total']}]({business_link})、[离线对照]({mock_business_link}) |

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
python -m src.review_evaluation --mode mock
python -m src.review_evaluation --mode live
python scripts/business_simulation.py --mode mock
python scripts/business_simulation.py --mode live
```

评估器为每个案例建立独立数据库，避免跨案例占位；duplicate 在两个独立 Python 进程中复用数据库。每次生成新的批次目录，不覆盖旧事件。保存异常案例会显式注入提交前故障。单独体验该案例：

```powershell
python -m src.main --mode mock --input examples/save_failure.input.json --db runs/failure.db --simulate-save-failure
```

若手动用默认共享数据库运行不同请求，相同渠道市场时间可能被之前的 READY 草稿占用，这是业务规则生效。重复请求会重新检查已有草稿的当前时段：仍有效则 ALREADY_EXISTS；失效的待确认建议可在模型查询新时段后，以同一 ID 重排，返回 SAVED / REPLANNED 并保留历史。已批准草稿失效时明确要求处理，不会静默重新批准。新 request_id 是独立需求。

## 更换已有排期数据

`data/schedules.json` 是初始化种子，不会在启动时覆盖数据库里的调整。修改源文件后，显式同步：

```powershell
python -m src.main --sync-schedules data/schedules.json --db runs/drafts.db
```

此命令不调用模型；按 schedule_id 原子更新提供的时间和状态，省略的 ID 保留，取消需显式填写 CANCELLED。整批校验失败时不更新任何排期。随后用同一个 --db 运行 Agent，即可验证变化后的冲突和重复提交。

## 如何读结果

CLI 输出 JSON 包括审核证据、原时间、建议时间、待确认项、schedule_blockers、当前 schedule_check、真实保存结果、执行状态、实际 token usage 和 Trace 文件路径。缺少授权调整窗口的冲突返回 NEEDS_CONFIRMATION，并列出所需输入。每次运行有 UUID run_id，生成 `events.jsonl`、`trace.json`、`result.json`；最终批次还记录代码与数据哈希。

审核状态为 PASS / NEEDS_INFO / NEEDS_REVIEW / BLOCKED。保存工具为 SAVED / ALREADY_EXISTS / SAVE_FAILED / REJECTED_BY_POLICY。草稿 READY 与 AWAITING_CONFIRMATION 均未发布；硬性时间的替代必须待用户确认。相同渠道市场、开始时间相距严格小于 60 分钟发生冲突；READY 草稿占位，待确认建议不占用其他请求的时段，但同一请求内的建议必须互不冲突。

最终结果区分当前审核结论与最近一次保存结果：素材变化、缺失或审核异常后，不再沿用先前的 DRAFT_SAVED 推荐。历史草稿仍保留在数据库；save_error 给出保存拒绝或失败原因，review_error 给出证据读取或审核异常，恢复后需重新审核再重试。

COMPLETED 表示所有素材得到明确处理结果，可能包含阻断或保存失败。退出码：0 已处理、2 配置或运行失败、3 未完成或预算耗尽、4 保存失败。`--max-steps`、`--max-tool-calls`、`--timeout` 可限制执行；工具非法参数也计入预算。

显式确认通过原始输入的 user_confirmations 提交，模型不能自行添加。精确确认会写入草稿证据，并在保存前重新检查冲突。字段说明及信任边界见 [技术方案](docs/02-solution.md)。

## 文件与文档

- `src/agent.py`、`llm.py`：实际模型决策循环及原生协议。
- `tools.py`、`rules.py`：工具注册、素材补查与确定性审核。
- `guard.py`、`scheduler.py`、`storage.py`：确认边界、冲突校验与 SQLite 事务幂等。
- `tracing.py`、`evaluation.py`：观测、脱敏、来源快照和实际验收。
- [需求分析](docs/01-requirements.md)、[技术方案](docs/02-solution.md)、[评估报告](docs/03-evaluation.md)、[AI 使用记录](docs/04-ai-usage.md)、[真实复盘](docs/05-retrospective.md)。
- [初次开发时间记录](docs/development-record.json)、[最新证据索引](docs/latest-evidence.json)、[GitHub 交付说明](docs/06-delivery.md)、[审核修复说明](docs/07-review-fixes.md)、[状态一致性修复](docs/08-state-consistency-fixes.md)、[业务模拟报告](docs/09-business-simulation.md)。

限制：当前是单机、合成元数据审核；没有视觉审核、真实许可查询、用户身份认证或生产发布。真实样本数量有限，案例通过不代表生产可靠性。CI 配置随仓库提供，实际远程执行状态以 GitHub 为准。

本地交付核查：`python scripts/verify_delivery.py`，扫描已暂存或跟踪文件的凭据及证据完整性。密钥、本地数据库、虚拟环境、缓存和运行目录都被 `.gitignore` 排除。
"""
    (ROOT / "README.md").write_text(readme, encoding="utf-8")
    labels = {"confirmation_lifecycle": "待确认 → 显式确认 → 重复提交", "confirmation_slot_changed": "确认后时段被占用",
              "batch_pending": "同请求两素材建议避让", "japan_localization": "日本市场与东京时区", "manual_claim_review": "未核实声明转人工"}
    business_rows = []
    for case in business["cases"]:
        link = portable(case["traces"][-1]["trace"]) if case.get("traces") else business_link
        business_rows.append(f"| {labels[case['case_id']]} | {'通过' if case['passed'] else '失败'} | [实际 Trace](../{link}) |")
    local_start = datetime.fromisoformat(business["started_at"]).astimezone(ZoneInfo("Asia/Taipei")).isoformat()
    local_end = datetime.fromisoformat(business["finished_at"]).astimezone(ZoneInfo("Asia/Taipei")).isoformat()
    business_document = f"""# 模拟运营流程与真实模型测试

本轮测试在当前状态一致性更新上实际执行。素材和用户操作是合成测试输入；live 中的工具选择由真实 DeepSeek deepseek-flash 产生，SQLite 查询、同步、提交和回滚都真实执行。没有调用真实内容发布接口，也没有把模拟确认当作真实运营批准。

模拟运营批次时间（Asia/Taipei）：{local_start} 至 {local_end}。

| 验证层次 | 真实模型结果 | 离线对照 |
|---|---|---|
| 六项强制场景 | [{live['passed']}/{live['total']}](../{relative_live}) | [{mock['passed']}/{mock['total']}](../{relative_mock}) |
| 审核动态边界 | [{review['passed']}/{review['total']}](../{review_link}) | [{mock_review['passed']}/{mock_review['total']}](../{mock_review_link}) |
| 模拟运营链路 | [{business['passed']}/{business['total']}](../{business_link}) | [{mock_business['passed']}/{mock_business['total']}](../{mock_business_link}) |
| 确定性工程测试 | {match[1]} 项通过，{match[2]} 秒 | [原始结果](../examples/test-results.txt) |

这是有限场景的实际断言结果，不是总体成功率。六项场景覆盖正常、授权缺失、版权阻断、冲突、跨进程重复提交与保存失败；后者使用明确标注的真实事务提交前故障注入。审核证据源异常、缓存失效与并发竞争由受控工程测试验证，不声称接入了真实外部素材系统。

## 五个模拟运营场景

| 操作 | 本批实际断言 | 证据 |
|---|---|---|
{chr(10).join(business_rows)}

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

工程测试的本机实际命令见 [test-session.json](../examples/test-session.json)。最新运行时来源指纹为 `{snapshot}`，交付检查同时核对真实与离线批次的每个来源快照；历史记录保留为对应版本证据。用户要求上传的状态一致性与路径更新已一并复验，见 [修复说明](08-state-consistency-fixes.md)。
"""
    (ROOT / "docs/09-business-simulation.md").write_text(business_document, encoding="utf-8")
    metadata = {"report_generated_at_utc": datetime.now(timezone.utc).isoformat(),
                "client_timezone": "Asia/Taipei",
                "pytest_passed": int(match[1]), "pytest_measured_seconds": float(match[2]),
                "latest_live_report": relative_live, "latest_mock_report": relative_mock,
                "source_fingerprint": snapshot, "review_live_report": review_link, "review_mock_report": mock_review_link,
                "business_live_report": business_link, "business_mock_report": mock_business_link}
    (ROOT / "docs/latest-evidence.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    main()
