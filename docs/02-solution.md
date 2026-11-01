# 实际实现

## 调用关系

```mermaid
flowchart TD
    CLI[CLI / immutable ScheduleRequest] --> Runtime[AgentRuntime]
    Runtime --> LLM[OpenAI-compatible Chat Completions]
    LLM -->|selected tool + JSON arguments| Registry[ToolRegistry / Pydantic]
    Registry --> Material[MaterialService + synthetic supplement]
    Registry --> Rules[ReviewService / deterministic rules]
    Registry --> Query[ScheduleService]
    Registry --> Guard[PolicyGuard]
    Guard --> Repo[DraftRepository / SQLite]
    Repo -->|BEGIN IMMEDIATE + conflict recheck + commit| DB[(SQLite)]
    Registry -->|actual observations| Runtime
    Runtime --> Trace[append-only events + compact trace + result]
```

## 模型决策循环

`src/agent.py` 每轮把完整的系统指令、原始请求、此前 assistant 工具调用及对应 tool 观察提交给模型。`src/llm.py` POST `/chat/completions`，传 `tools` JSON Schema、`tool_choice=auto`、`parallel_tool_calls=false`。模型选择工具、参数或结束；运行时校验并执行，然后把真实工具响应追加回上下文。

运行时没有“发现 18 点冲突则自动改 19 点”的决策分支，没有模型调用重试。即使提供方返回多工具批次，工具按顺序执行且逐项计算预算。模型可以自主重新查询和重试保存。SQLite 的锁等待属于事务协调，不属于替模型规划。

时段查询会根据本次实际冲突的开始时间、配置的最小间距和用户窗口端点，计算 `candidate_time_hints`。所有提示明确标为 UNVERIFIED，不代表空闲或批准。模型仍须自行选择、查询并观察后才能保存；候选不是硬编码时间，也不会自动触发下一次查询。这用于避免模型只尝试五分钟偏移而漏掉恰好满足最小间距的边界。

READY 草稿对同渠道市场的所有请求占位。AWAITING_CONFIRMATION 草稿仅在同一 request_fingerprint 内参与其他素材的冲突检查，防止一批建议互相重叠；它们不对独立请求占位。查询与保存事务使用相同规则，并发保存同批互相冲突的建议时仅允许一项成功。

`reason_summary` 是动作的简短公开目的，不请求私有推理链。适配器不读取或保留 `reasoning_content` 等隐藏推理字段。实际 DeepSeek 运行显式使用 `thinking.type=disabled`，通过 `LLM_EXTRA_BODY` 配置；额外配置禁止覆盖消息、模型和工具等关键字段。

协议参考实际打开的 [OpenAI Function Calling](https://developers.openai.com/api/docs/guides/function-calling)、[DeepSeek Tool Calls](https://api-docs.deepseek.com/guides/tool_calls/) 和 [DeepSeek Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/)。本项目适配 Chat Completions；仅支持其他接口的模型不能直接替换使用。

## 工具与职责边界

| 工具 | 实际输入 | 权威上下文与行为 |
|---|---|---|
| read_material | ID、公开动作目的 | 从合成 JSON 返回真实记录，计算缺失字段 |
| lookup_material_info | ID、公开动作目的 | 只允许可信合成补查源修补许可字段；可能仍缺失，补查使旧审核失效 |
| check_review_rules | ID、公开动作目的 | 用已读取数据和原始请求执行确定性规则；不接受模型伪造的素材或审核状态 |
| query_schedule | ID、带偏移的时间、公开动作目的 | 渠道和市场绑定原始请求；返回实际冲突、可搜索窗口和间隔规则，由模型选择下一次查询 |
| save_schedule_draft | ID、建议时间、确认状态、公开动作目的 | 要求已读、已审、已查；重审，Guard 验证，数据库提交后才返回成功 |

模型不可改 request_id、市场、渠道、硬性约束或确认列表。未知工具、额外参数、非法 JSON、无偏移时间、越界素材、未完成前置动作都会产生结构化拒绝，并返回模型作为下一轮观察。

许可明确禁止或超出渠道市场范围 BLOCKED；许可未知和必填字段缺失 NEEDS_INFO；语言不符、未核实宣传声明与人工风险标签 NEEDS_REVIEW。规则优先级是 BLOCKED > NEEDS_INFO > NEEDS_REVIEW > PASS。

`ReviewMetadata` 对 has_promotional_claim 与 claims_verified 使用 StrictBool。非法字符串、整数等不能静默转换为审核通过。提供核实字段意味着存在宣传声明；null 表示未核实，必须人工复核。没有声明的元数据通过显式 has_promotional_claim=false 或省略声明字段表示。source、license_note 等扩展信息仍可保留。补查或重复读取只有在素材实际变化时才使旧审核失效，避免无变化补查清空已知审核结果。

## 时间与确认

模型只能在用户窗口内提出未确认替代；硬性时间的替代必须 PENDING，软偏好窗口内可 WITHIN_FLEXIBILITY。精确 `user_confirmations` 输入允许 USER_CONFIRMED 并持久化 material_id、target_time、confirmed_at、source。确认时间不能在未来或晚于目标时间。它是本地 CLI 的可信用户输入边界，没有实现身份认证或签名审批。

确认后必须再次执行整个读取、审核、查询和保存流程。新确认输入产生新指纹；旧的待确认建议保留为审计记录，不会被自动升级。新 READY 草稿仍要经过当下的冲突校验；若已被占用则拒绝保存。没有可变审批接口。

## 事务和幂等

原始 Pydantic 请求规范化为键排序 JSON，时间转换为 UTC，素材及确认列表排序，计算 SHA-256。request_id 也参与指纹：调用方必须复用同一个 ID 重试；新的 ID 表示独立需求，不被错误合并。数据库唯一键 `(request_fingerprint, material_id)` 支持多素材独立保存。

保存先执行 BEGIN IMMEDIATE 并查找同指纹记录。新草稿经过当前时段二次查询、Guard、INSERT 和 COMMIT；已有草稿按下述规则复查与复用或重排。锁覆盖校验与写入，避免两个进程同时占用同一时段。唯一约束提供幂等最终保证。SQLite WAL 与 10 秒 busy timeout 适合此单机最小实现。

修复后的幂等分支同样在事务内查询已有草稿的实际时间并执行 Guard。仍有效时 ALREADY_EXISTS，维持原内容；失效的 AWAITING_CONFIRMATION 建议可在模型查询新的合法目标后，UPDATE 同一行，返回 SAVED / action=REPLANNED。原 ID、原要求和 created_at 保留，revision 增加，旧内容写入 draft_revisions。已批准草稿失效时返回 REJECTED_BY_POLICY / EXISTING_DRAFT_INVALID，由用户明确处理，不静默修改其批准含义。新目标仍校验查询证据、时间窗口及确认状态。事务失败回滚更新和历史插入。

最终结果生成时还会读取当前记录并查询该时间，输出 schedule_check。若提交后状态已变化，则明确给出 NEEDS_CONFIRMATION、冲突及待办，不继续把失效时间放入 suggested_publish_time。校验结果是 checked_at 时刻的快照，不承诺未来永远不变。

当前审核证据同样影响最终结果：重新读取的素材发生变化后必须重新审核；读取返回 NOT_FOUND 会清除旧素材和审核缓存，后续恢复时清除 NOT_FOUND 标记。读取、补查或审核失败后清除旧审核结论，输出 review_error 和待办，不能复用旧 PASS。即便历史保存成功，当前缺失、风险或未完成审核也不能输出 DRAFT_SAVED 或建议时段。已有数据库记录保留为历史事实，不因缓存失效而删除。

save_status 表示最近一次保存调用的结果，保存前置检查失败也会更新该结果。save_error 保留结构化 code 和 message，包含实际存储错误码；因此后来发生的拒绝或异常不会被更早的成功结果覆盖。恢复证据源并完成读取、审核及保存重试后，仍由现有事务和唯一约束保证幂等。

`--simulate-save-failure` 在真实 INSERT 后、COMMIT 前抛出 SQLite 异常并回滚；这是明确的故障注入，不伪造持久化结果。最终输出还会再次读取已提交记录验证 ID；访问失败返回 INCOMPLETE 和验证错误，不能宣称已验证保存成功。

故障注入也覆盖重排 UPDATE 与旧版本插入之后、COMMIT 之前，测试验证二者一起回滚。无成功保存的冲突场景由实际查询观察生成 schedule_blockers；没有调整窗口或精确确认时，返回 NEEDS_CONFIRMATION，并说明需要 flexible_time_window 或 user_confirmations。不会通过解析或信任模型自由文本来生成批准状态。

## 正式排期数据同步

默认 JSON 是一次性初始化种子，INSERT OR IGNORE 防止每次进程启动覆盖数据库当前状态。更换源文件后必须显式运行 `python -m src.main --sync-schedules path/to/schedules.json --db runs/drafts.db`。该维护模式不调用模型，execution_mode=maintenance；按 schedule_id 原子 UPSERT 时间、渠道、市场和状态，整批校验带时区时间、状态和重复 ID 后才写入。省略的 ID 不删除，取消需显式提交 CANCELLED。同步后 Agent 使用同一数据库查询最新结果。同步是用户显式的输入维护操作，不属于模型行动规划。

## 执行预算与观测

默认最大 16 个模型决策轮次、24 次工具调用，CLI 可调整但有上界；非法调用同样消耗工具预算。还有最终总结预算时允许模型结束；若仍请求工具而额度已用尽则 INCOMPLETE。每次模型请求默认 45 秒 httpx 超时，网络、HTTP、协议、截断和适配器错误都有结构化结果，没有自动重试或 Mock 回退。

每次生成 UUID run_id。`events.jsonl` 追加来源快照、请求、模型响应标识、工具调用、完整必要观察、usage 和最终状态；`trace.json` 提供每一步的动作、公开依据、参数、返回摘要和下一实际动作；`result.json` 保存权威结果。来源快照对 src/*.py 和 data/*.json 逐文件计算 SHA-256，方便对应最终代码。原始事件不被评估脚本修改。

日志按环境中已知密钥和 `sk-` 模式脱敏，不记录 HTTP Authorization、提供方错误响应正文或无关完整上下文。实际 token usage 原样保留；缺失标 `unavailable`。最终自然语言属于非权威记录，业务状态以结构化输出和数据库为准。

## 取舍

原生 Tool Calling、Pydantic、httpx、SQLite、pytest 足够验证核心能力，因此没有引入 Agent 框架、前端、RAG、MCP、视觉模型或多 Agent。MockModel 只是一套明确标注的脚本模拟器，验证相同工具与存储链路，不证明真实 Agent 能力。每个评估场景使用独立数据库，避免不同场景互相占位；重复场景专门跨两个独立 Python 进程复用同一数据库。
