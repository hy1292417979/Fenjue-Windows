# 渠道与部署交付后续（2026-10-07）

按用户“更换方法”指示并行研究 BPS、原生CLI、SIWC。完整BPS契约两次403后暂停同类试探；原生CLI和直接HTTP同题均正确。SIWC助手抵达官方登录页，用户只有JSON，已核对其缺少direct scope与网页session，取消等待登录，保留后续授权入口。全套108测试OK（1POSIX跳过），Windows包全新目录离线安装、状态、启动/health/停止验收通过；含原生文本CLI与完整源码，凭据及运行账本不打包。具体证据见 `渠道进展_2026-10-07.md` 和 `RELEASE-VALIDATION.json`。

以下为前阶段记录。

# 站长协议研究后续（2026-10-07 前阶段）

以 `站长接入与机制研究_2026-10-07.md` 为当前交付说明。下面旧记录保留其历史时间语境。

- Sub2API 原生 WS 已接通，账号 1 / 分组 8，用量 39 确认 openai_ws_mode=true；Astra/max 新题答对，38.219 秒。HTTP 用量 37 仍标记 false。
- 本阶段 12 次客户端提交已记账（4+6+1+1），最终失败／成功均保留；宿主上游尝试次数没有用该数冒充。临时 Key 8—11 均已删除并统一核对。
- WS 两个同类配对题与 HTTP 均正确，尚未出现稳定能力优势。BPS 最小探针 403，完整公开插件存在 header、metadata、工具协议差异，继续列为未验证候选。
- 主入口切回站长状态／恢复工具；17877 辅助工作台停止，客户入口继续 Sub2API。
- 独立审查发现 WS 自动重定向携带凭据与缺少 websockets 依赖，均修复。真实 loopback 回归先失败再通过。
- 原生源码证实 PUT extra 全量替换；operator 改用单账号 bulk-update 原子合并，新增并发无关字段保留回归。原缺失键以 null 恢复，在所核三字段上原生读取等效；同键并发编辑仍为后写覆盖。
- 最终 58/58 离线测试通过，compileall 与 pip check 通过；102 个源码／证据／日志文件未匹配所选账号真实凭据。单账号 bulk 增量接口同值更新已在本机验证，17877 已停止，18080 正常监听。

---

# SDD ledger — plan: docs/superpowers/plans/2026-10-07-fenjue-route-discovery.md

Spec and plan approved. User selected native execution and no intermediate approval stops.

Pre-flight: task 1 public task/oracle interfaces feed task 4; task 2 StudyDB feeds task 4; task 3 CallResult feeds tasks 4/5. No signature conflicts.

Ruling: use isolated new project directory on codex/fenjue-discovery in the existing unborn repository; no worktree can be based on a nonexistent commit. Existing user files stay untracked. Cost: directory isolation rather than separate checkout.

Ruling: use a dedicated discovery entry point; preserve legacy research adapters without promoting them to validated local routes. Cost: legacy entry points remain explicitly experimental.

Task 1: source restored with archive hashes; Python 3.12 venv and pinned HTTP dependencies installed. Tests pending.

Task 1: complete — source provenance, pinned requirements.lock, 4 oracle tests pass. Legacy gateway dependencies moved to an optional extra.
Task 2: complete — frozen material/manifest and persistent ledger tests RED→GREEN (4 tests).
Task 3: complete — strict SSE, local exclusive binding, cleanup and usage audit tests RED→GREEN (9 tests).
Task 4: complete — reserve-before-send, interruption/resume, local tool continuation tests RED→GREEN (7 tests).
Task 5: complete — offline commands, missing comparisons and ceiling reporting tests RED→GREEN (3 tests).

Ruling: offline prepare freezes materials; inventory binds the explicitly selected local identity before run, which rechecks before/after each call — preserves genuinely offline preparation — cost: a separate inventory command is required before execution.
Ruling: screening cap is 36 calls, confirmation cap is 96 per pair in one time window — derived from task max_turns rather than old 300-call defaults — cost: cross-window confirmation requires a separately frozen study.
Ruling: restored coding oracles grade synthetic SQL, not real repository patches; disclose this coverage gap and retain future repository tasks as a limitation — no false claim of broad coding validation — cost: current capability inference is narrower.

Offline suite: 27/27 pass; discovery compileall passes. Task 6 in progress; real inference used by new discovery tool: 0.

Task 6 live acceptance: exactly 2 calls consumed. Codex HTTP returned 200 but not SSE per content-type gate (no body persisted); Sub2API completed correct JSON, 91 input / 17 output tokens. Dynamic usage metadata in routing fingerprint produced a false identity gap. SQL cleanup verification returned PostgreSQL t instead of JSON. Temporary key 3 deletion verified by read-only database query.
Fixes: controls_fingerprint excludes enumerated usage telemetry but retains routing options; cleanup SELECT uses to_json; final_answer phase excludes commentary. Regression tests RED→GREEN. Original call/outcome records remain unchanged; supplemental audit records local usage confirmation separately.
Ruling: keep the two-call acceptance incomplete and preserve first records; fix code with offline regression tests without retrying live inference — stays within user cap and avoids cherry-picking — cost: direct HTTP and revised code lack a successful new live acceptance.
Delivery: wrapper, read-only launcher, README, STATUS and frozen 36-call screening proposal; no formal screening authorized or executed.

Task 6 execution finished: 31/31 offline tests pass, compileall passes, PowerShell wrapper verified; real acceptance incomplete (2/2 consumed). Post-hoc local usage matches account/model/effort and key deletion verified; 56 project/evidence files scanned with zero matches for the selected real tokens/passwords. Official CLI version 0.160.1 queried with explicit executable, no login or inference. Desktop shortcut created, report opened. Screen proposal ledger 0/36.
Ruling: repository had no Git author configured; commits use command-local Codex <codex@local.invalid> attribution — avoid changing user's global Git identity or impersonating their email — cost: commits carry a synthetic agent author.
Final review: fresh independent reviewer dispatched against root commit 529a8d4; awaiting findings. No active scratch worktree was created and no original research is included in Git.

Final review returned: 0 Critical, 2 Important, 0 Minor. Re-graded both Important and fixed in one pass.
Final: fixed protocol failures entering quality comparison — test_protocol_failures_are_not_quality_comparisons RED→GREEN (not_sse, timeout, model mismatch, exhausted budget); suite 33/33.
Final: fixed canonical SQL answer rejected by SQLite authorizer — test_all_sql_reference_answers_pass_restricted_grader RED→GREEN at confirm-coding-07; all 11 reference queries pass; suite 33/33 and compileall green.
Final: Ruling: legacy experimental gateway/adapters remain outside current validated scope — new entrypoint is isolated and docs identify the boundary — cost: old entrypoints lack new-entrypoint guarantees.
Final: Ruling: actual model capability differences remain undetermined — smoke-only scope has no distinguishing benchmark or trusted baseline — cost: no full-capability answer yet.
Final: Ruling: unresolved direct HTTP live cause stays documented as incomplete acceptance — new live calls exceed approved two-call cap — cost: direct route lacks new-version live validation.
Final: Ruling: CLI/WS/BPS/SIWC readiness stays not_integrated — no eligible isolated credentials/protocol verification — cost: no production guarantees for these candidates.
Final: no deferred minors.
Ruling: keep the local codex/fenjue-discovery branch and working directory as delivered; skip finishing-skill integration menu under user's explicit instruction to complete without incremental confirmations — no base commit or requested remote publication — cost: integration/publishing remains a future separate action.

## 2026-10-07 follow-up: user requires empirical evidence

User clarified: only this account is available; first establish differences between the two channels. Announced maximum 36 new calls. No healthy-account or paid API reference was used.

Read-only catalog GET on HTTPX and Node both returned 200 JSON, listed 10 models including gpt-6-astra, no inference. Screening tasks/order/budget reused the already frozen 12-task proposal. Pre-registration and implementation hashes saved before calls; stop-on-protocol-error enabled.

Call 1 revealed actual SSE data with no Content-Type. Added structurally validated unlabelled SSE support and full JSON Responses handling, with strict terminal checks and byte bounds. Call 1 retained as failed and never replayed. Revision 2 hashes registered before call 2.

Two tool outputs were empty after terminal-only extraction. Paused process during call 11 (unknown completion remains charged), verified process ended, removed only its stale lock and deleted its dedicated key 5. Added output_item.done assembly, conflict checks and rejection of completed-with-no-actionable-output. Two earlier empty tool results excluded from quality rather than treated as model errors. Revision 3 hashes registered before call 12. No submitted question was replayed.

Screen finished at 32 reserved calls: 8 valid pairs spanning 5 families, direct 8/8 and Sub2API 7/8. Sub2API's checksum 127 versus independently recomputed 125 was the sole paired difference. Remaining four of the announced 36 calls used on the first two unseen confirmation constraint tasks, with a new frozen subset and balanced order. Holdout result: Sub2API 2/2, direct 1/2. Direct's second answer violates clues 3,4,6; independent enumeration of all 5040 permutations finds exactly one valid order, matching Sub2API. This is a unique-order constraints task, not numeric optimization; an intermediate commentary misdescription was explicitly corrected.

Total new calls charged 36/36, including one unknown completion; 10 complete pairs overall, each route 9 correct (descriptive only; stages remain separate). Opposite-direction errors do not establish stable channel superiority. No full-capability claim. Temporary keys 4,5,6,7 verified deleted. 81 project/evidence files scanned with zero real selected-token/password matches.

Verification: new transport and fail-fast regressions RED→GREEN; all 39 offline tests pass; compileall passes. Task-specific independent checksum and permutation verifiers pass. Latest consolidated report generated at 证实结果_2026-10-07.md, desktop launcher now points to it. Original frozen materials and first outcomes remain intact; supplements/exclusions are separately recorded.
