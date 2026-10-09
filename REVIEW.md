# 原生渠道、SIWC 与部署包审查（2026-10-07）

本轮独立审查发现并已修复：原生客户端父进程先退出后子进程仍持认证目录（Windows suspended spawn + Job、POSIX process group）；大输出管道清理死锁；SIWC慢速连接延长截止与accept注册竞争；启动页no-referrer导致浏览器POST Origin:null；Windows PowerShell双引号转义和解释器探测提前中止；既有venv版本检测；wheelhouse缺失静默联网；宿主PIP_TARGET污染安装目标。

登录采用严格OIDC验签与同账号核对，新凭据独立存储，宿主refresh token留在Sub2API。完整108项测试OK（1项POSIX专用跳过）。真实浏览器到达官方登录页；原生CLI真实单题对照与安装/启动验收见最新渠道记录。凭据JSON缺SIWC direct scope，未取得新grant，不归类为生成成功。

旧审查fixture空目录的清理曾被自动审批以 `blocked by policy` 拦截，未换命令绕过；当时临时auth文件已删除，子进程自行结束。

以下为历史审查记录。

# 站长协议扩展审查（2026-10-07 前阶段）

以 `站长接入与机制研究_2026-10-07.md` 为当前交付说明。下面旧记录保留其历史时间语境。

最终补充独立复审：已核对 normalizeBulkOpenAISettings 与 ShouldEnsureCodexFingerprintSeedForExtraUpdates；仅传三个 WS 字段不触发额外设置归一化或 fingerprint seed 创建。没有遗留 Critical／Important 项。

- Sub2API 原生 WS 已接通，账号 1 / 分组 8，用量 39 确认 openai_ws_mode=true；Astra/max 新题答对，38.219 秒。HTTP 用量 37 仍标记 false。
- 本阶段 12 次客户端提交已记账（4+6+1+1），最终失败／成功均保留；宿主上游尝试次数没有用该数冒充。临时 Key 8—11 均已删除并统一核对。
- WS 两个同类配对题与 HTTP 均正确，尚未出现稳定能力优势。BPS 最小探针 403，完整公开插件存在 header、metadata、工具协议差异，继续列为未验证候选。
- 主入口切回站长状态／恢复工具；17877 辅助工作台停止，客户入口继续 Sub2API。
- 独立审查发现 WS 自动重定向携带凭据与缺少 websockets 依赖，均修复。真实 loopback 回归先失败再通过。
- 原生源码证实 PUT extra 全量替换；operator 改用单账号 bulk-update 原子合并，新增并发无关字段保留回归。原缺失键以 null 恢复，在所核三字段上原生读取等效；同键并发编辑仍为后写覆盖。
- 最终 58/58 离线测试通过，compileall 与 pip check 通过；102 个源码／证据／日志文件未匹配所选账号真实凭据。单账号 bulk 增量接口同值更新已在本机验证，17877 已停止，18080 正常监听。

---

# 独立审查及修复记录

2026-10-07，由一个新上下文的独立代码审查代理检查提交 `529a8d4` 的 discovery 实现、计划、测试及复用的评分/存储接口。审查只运行离线测试，没有访问账号凭据或真实模型。

发现 0 项 Critical、2 项 Important，没有 Minor：

1. `report.py` 原先仅凭两端有结果且身份符合就计入质量比较，可能把超时、not_sse、模型不符、预算耗尽当作对手能力较差。现要求两端都有 `completed` 终态再统计配对胜题，协议错误单独保留为可靠性证据。
2. `research/tasks.py` 的 SQLite 授权回调误拒一种合法的已知表扫描（空列名、数据库名为空），导致标准答案 confirm-coding-07 全部用例失败。现仅为已知 fixture 表的这个具体回调放行，其他表、写操作和扩展加载仍拒绝。

修复前分别复现失败；修复后 **33/33** 测试通过，其中全部 **11 道 SQL 标准答案**通过受限评分器。修复者完成一轮回归验证，没有另做第二轮代理审查。

审查保留范围已由实现者裁定：

- 旧实验网关/适配器维持隔离，不作为本版已验收能力；代价是旧入口不受新入口的完整保证。
- 模型真实能力差异保留为未研究；两次接入检查不回答此问题。
- 直接 HTTP 的真实响应原因仍未确定；两次真实额度已用完，代价是该路径尚未完成新版本接入验收。
- CLI/WS/BPS/SIWC 只登记条件与缺口；代价是这些路径还没有生产可用保证。

实际验收及预算见 `STATUS.md`。不存在已发现“满血渠道”的结论。
