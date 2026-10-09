# 焚决 · Cockpit 账号的 Codex 接入保护

## 当前 v3：开启才接管 Cockpit 出站

最新链路已变为 **Codex → Cockpit → 焚决 → 固定 GPT OAuth 官方上游**。

- 开：`焚决.cmd` 或 `Start-Guard.cmd`。
- 关：`Stop-Guard.cmd`。确认 Cockpit 解除接管、在途请求结束后才停止保护层。
- 看状态：`Status-Guard.cmd`。关注 `takeover_active`、`guard_ready`、`installed_binary_matches` 和最近 `cockpit_outbound` 调用。
- 恢复原版 Cockpit 转发程序：`Restore-Cockpit.cmd`（会重启 Cockpit）。原版保存在 `runtime/guard/original-cockpit-cliproxy.exe`，恢复前校验哈希。

Codex 地址保持 Cockpit 的 64492；第一次从旧版本迁移后请重启 Codex、新开任务。开关不再争抢 Codex 配置。已验证关闭/开启的配置文件字节完全一致，也已验证 Cockpit 重启后接管仍然有效。同次开机内保护层异常退出不自动放行，需重新开焚决或用停止入口解除接管；Windows 重启后旧开关自动失效，没开焚决就不接管。目前没有开机自启。

完整范围、真实验证与限制见 [COCKPIT-OUTBOUND.md](COCKPIT-OUTBOUND.md)。下面保留 v2 前置网关的历史验收，不代表当前入口或新链路完整验收。

## 历史 v2 状态：压缩兼容修复与前置网关

2026-10-09：原来的聊天网页不符合本次目标。本版把原焚决的账号固定、原生路由优先、返回校验、工具及推理状态续接机制迁移到 Cockpit。

初轮 4 次真实验收中，工具调用/续轮成功，但旧 `/responses/compact` 返回 HTTP 404。用户明确授权再增加最多 4 次。追加第 1 次命中仍运行的旧服务，计入失败；重启新版后，连续 3 次完成了原生加密压缩、凭压缩状态回忆随机标记并调用工具、接收工具结果后精确作答。共 8 次提交，无重置账本、无超额调用。

已仅将 Codex 当前 Cockpit provider 的 base_url 改为 `http://127.0.0.1:17878/v1`；对其余所有配置进行比对，确认未改模型 gpt-6-astra、思考强度 medium、密钥、MCP 或其他配置。服务现已启动。请完全退出再打开 Codex，并开始新任务；当前已运行的任务可能仍持有原接入地址。

不能将此状态描述为已经全面防止“降智”。原发布包也明确写着能力提升尚未证实。参数正确、请求成功与回答质量提升是不同的结论。

## 实际接入链路

当前链路：Codex → 本机焚决 17878 → 固定 GPT 账号的原生 Codex 上游。

Cockpit 仍负责账号登录、授权刷新和提供已有账号引用。焚决每次读取 Cockpit 刷新后的凭据，不复制 OAuth token 到项目、不自行刷新、不安装 Sub2API、不调用 CPA。

这里绕开的是 Cockpit 的请求转换和多账号调度，不是让“端口”具备额外能力。直连请求不会出现在 Cockpit 的代理请求统计里，仍会消耗对应 GPT 账号额度。

## 从原焚决迁移的机制

- 用 `cockpit_accounts.py` 读取 Cockpit 账号和本地凭据，替代原 Sub2API 数据库发现。首次固定现有唯一首选 OAuth GPT 账号；以后顺序、首选项变化不会静默换号。账号删除、身份替换或令牌过期时停止请求，由 Cockpit 恢复登录/刷新。
- 复用 `service.choose_route`：没有有效同账号对照结果时优先原生 HTTP。短题对照报告有账号、模型、思考强度与时效限制，不当作能力提升证明。
- 当前 Cockpit 密钥覆盖多个账号，因此禁止宣称其为同账号对照，也禁止自动回落到混合池。只有密钥独占绑定当前账号时才允许两路检查。
- 保留 instructions、tools、完整 input、加密 reasoning、configuration_update；明确设置的思考强度保持原样。只在客户端未填写时沿用原焚决默认 max，模型不支持时明确报错，不降低档位重试。
- 复用原 SSE 终态/条目核对逻辑，兼容 custom_tool_call，检查模型与返回的请求级思考强度。configuration_update 改变有效强度时，返回元数据仍报告请求级设置；保留更新，不误判为降级，且有效强度仅记为请求值、未获独立验证。元数据缺失只记为“未验证”。
- 旧压缩入口转换为原生 `/responses` 加末尾 compaction_trigger，一次调用完成，不先请求旧地址再重试。保留全部加密输出项，不使用文字摘要替代。客户端直接发送新版压缩触发项也可原样通过。
- 使用 Codex 标准 session_id/conversation_id/prompt_cache_key 绑定会话。同一会话不能静默更换账号或路由。
- 校验完成后才释放响应和工具事件；等待时发送保活消息。保留 x-codex-turn-state 等续接头。代价是文字不再逐字即时出现。
- 不增加“自我复核”模型调用，不重写提示词，不自动重试或降级模型。每次请求最多 300 秒、正文/返回各 32 MiB，不默默截断历史。

原生模型目录仅来自选定账号；Cockpit 聚合目录中的其他厂商模型不属于本保护层支持范围。当前 Codex 静态目录尚未改动，后续启用时应只选择该 GPT 账号实际可用的模型。

## 入口与回滚

已完成本机限定协议验收，入口会检查通过记录与当前账号绑定是否一致。日常操作：

1. 保持 Cockpit 的本地接入及账号刷新可用。
2. 双击 `焚决.cmd` 或 `Start-Guard.cmd`：启动保护层，并只修改 Codex 当前 Cockpit provider 的 base_url。保留模型、思考强度、密钥、MCP 和其他配置。
3. 完整退出再打开 Codex，开始新任务。原会话的账号来源无法追溯，不建议迁移携带旧账号推理状态的长会话。
4. 双击 `Stop-Guard.cmd` 或 `Stop-Workbench.cmd`：先恢复 Cockpit 地址，再停止焚决。Cockpit 本身不停止。恢复后重新打开 Codex。

启动/停止/状态查询不消耗生成额度。没有设置开机自启，重启电脑后先开 Cockpit，再双击焚决。Cockpit 如果重新写回 Codex 配置，需要再次显式启动启用；保护层不会后台争抢配置。若启动提示旧版占用端口，先停止再启动，不会把旧服务当作新版验收。

只启动服务、不修改 Codex 的开发入口：`scripts/Start-Guard.ps1`。接口是 `http://127.0.0.1:17878/v1`，仅监听本机。认证接受本机 Cockpit key 或受权限保护的焚决 key，不需要重新登录 GPT 账号。

旧 `Start-Cockpit.cmd` / `Stop-Cockpit.cmd` 是 17877 聊天演示；原 Sub2API 状态入口为 `Sub2API-Status.cmd`。这两者不是当前 Codex 接入保护的入口。

## 数据及安全限制

- `runtime/guard` 限当前 Windows 用户和 SYSTEM；不要分享该目录。
- settings 仅保存账号引用，不保存 OAuth token。日志/账本仅存哈希、调用状态和校验元数据，不保存提示词、工具输出、原始推理或账号凭据。
- `codex-url-rollback.local.json` 仅记录原 URL 和配置定位信息，不备份整份含密钥的 Codex 配置。只在 URL 仍归本工具所有时恢复，避免覆盖用户后来修改。
- 复杂模型别名、限制或重映射当前采取拒绝接入，不绕过策略。HTTP(S) 代理沿用选中账号/Cockpit 的现有设置。
- 不提供 WebSocket 代理或 Chat Completions 转换；不支持 store:true、previous_response_id、后台请求。要求完整 Responses 历史。
- 取消、断线和失败可能仍消耗额度。服务不自动重试，但 Codex 自身可能按客户端策略重试。

## 验收记录

- 新保护层与配置编辑离线测试：24/24 通过；加上旧 Cockpit 后端为 34/34。
- 最新全 Python 测试：142 项，140 通过、1 跳过、1 原 SIWC Windows ACL 测试失败。该失败在本次改造前存在，相关模块未修改，不得称全部通过。新增两项压缩回归测试也通过。
- 编译检查与新 PowerShell 语法检查通过；启动/停止服务通过，Cockpit 保持运行。
- 原生账号目录返回 10 个模型。真实测试使用 gpt-6-astra、medium；工具调用和精确标记续轮成功，返回模型/effort 一致。
- 普通短请求未返回 encrypted reasoning 条目，此类 reasoning 保留仍由离线夹具验证；新版压缩确实取得 encrypted compaction 状态，并完成携带该状态的记忆/工具续轮。
- 长对话协议的短夹具验收已通过；没有消耗巨大上下文额度测试极限容量。客户端直接发送新版压缩流由离线测试覆盖，真实测试覆盖旧入口到新版协议的转换。
- 总预算 8 次已使用完毕，保留所有成功/失败记录。没有追加生成请求进行回答质量评分。
- 未做健康同账号多题质量对照，未验证能力提升；未验收所有 Codex 功能。

离线重跑：`.venv/Scripts/python.exe -X utf8 -m unittest tests.test_cockpit_guard tests.test_guard_config tests.test_cockpit -v`。

真实测试脚本 `fenjue.guard_smoke` 要求显式的累计授权预算参数，并在提交前持久记录次数。当前预算已耗尽，不能通过重复运行重新取得额度。测试开始前先验证服务版本，避免再次误测旧进程。

官方接口参考：[Compaction](https://developers.openai.com/api/docs/guides/compaction)、[Reasoning / configuration_update 与 compaction_trigger](https://developers.openai.com/api/docs/guides/reasoning)。本次依据 OpenAI Docs 修正压缩协议及请求级/有效思考强度的校验语义。
