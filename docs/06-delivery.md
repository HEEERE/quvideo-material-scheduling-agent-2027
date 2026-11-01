# GitHub 交付与复核

仓库：[HEEERE/quvideo-material-scheduling-agent-2027](https://github.com/HEEERE/quvideo-material-scheduling-agent-2027)。可见性：**私有**。主分支：`main`。私有仓库的评审访问权限由账号所有者配置；未擅自邀请任何第三方。

精确版本以当前仓库提交为准，最终交付消息给出实际远程核对的 commit SHA。可以自行执行：

```powershell
git rev-parse HEAD
git ls-remote origin refs/heads/main
gh repo view HEEERE/quvideo-material-scheduling-agent-2027 --json url,visibility,defaultBranchRef
gh run list --repo HEEERE/quvideo-material-scheduling-agent-2027
```

本地执行证据、确定性工程测试、源文件快照和最终复核结果均在仓库内；GitHub 工作流是额外的跨平台检查，状态以 Actions 实际结果为准。

提交前核查使用 `scripts/verify_delivery.py`：扫描暂存文件中的实际环境密钥及常见 key 模式；拒绝 `.env`、运行数据库和 runs 目录；检查最终 live/mock 各六场景通过，并逐文件比对最终 Trace 的源码哈希。

`.gitattributes` 固定源码为 LF，避免跨平台换行符导致来源哈希不一致。原始运行证据按二进制属性保留字节，不做换行规范化。历史探索记录和失败恢复记录保留原样。

评审索引和核查脚本会规范化 Windows 证据记录中的路径分隔符，文档使用 POSIX 相对链接；CI 同时在 Linux 和 Windows 运行交付核查。

虚拟环境、`.env`、SQLite 数据库和运行缓存保留在本机忽略目录。评估报告提供真实保存记录的数据库快照；评审可用 README 命令重新生成数据库并验证幂等行为。没有提交真实 API Key、真实公司数据或本机完整临时错误堆栈。

AI 辅助和人工贡献范围见 [AI 使用记录](04-ai-usage.md)。项目当前限制见 [评估报告](03-evaluation.md)。未添加发布接口。

审核修复交付新增 [修复说明](07-review-fixes.md) 与 [最新证据索引](latest-evidence.json)。交付核查同时检查真实/离线的六个基础场景和两个审核动态边界，并逐条检查最终运行的源文件哈希；初次开发的时间记录保持为历史记录，不累计为本次修复工时。
