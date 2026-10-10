# 焚决 v3：Cockpit 出站保护

2026-10-09：已按原焚决的原生路由优先、严格终态校验和完整状态续接逻辑增加 Cockpit 适配，不需要 Sub2API。目标是给 Codex 使用，不是额外聊天网页。使用和当前限制见 [COCKPIT.md](COCKPIT.md)。

**当前采用出站接管：Codex → Cockpit → 焚决 → Cockpit 当前选中的 GPT OAuth 账号官方上游。** Codex 保持连接 Cockpit（本机目前为 64492），不再以 17878 作为客户端入口。`焚决.cmd` / `Start-Guard.cmd` 开启接管；`Stop-Guard.cmd` 先解除并等待在途请求结束，再停焚决；`Status-Guard.cmd` 查看实际接管状态。开关不会改 Codex 地址。首次迁移请重启 Codex 并新开任务。

已备份并安装针对 Cockpit 1.3.66 的转发程序补丁；可用 `Restore-Cockpit.cmd` 恢复原程序。重启 Cockpit 保持接管；重启 Windows 后旧开关自动失效，没有开启焚决就走 Cockpit 原出站。Cockpit 更新可能覆盖补丁，请运行状态入口检查。同次开机内异常杀掉焚决不等于正常关闭：会报错而不是静默绕过，重新开启焚决或运行停止入口恢复。没有设置焚决开机自启。

仅保护选定 Cockpit 接入密钥的 GPT/Codex HTTP Responses 及压缩请求，其他供应商和语音/图片独立接口不在保护范围；WebSocket 不支持。旧 `Start-Cockpit.cmd` 只是 17877 聊天演示。协议检查和请求成功不等于回答质量提升证明。实现、验收和限制见 [COCKPIT-OUTBOUND.md](COCKPIT-OUTBOUND.md)。

`Install.cmd` 默认安装 Cockpit 依赖。原 Sub2API 状态入口保留为 `Sub2API-Status.cmd`，原研究代码与记录仍保留。下面的 Sub2API 地址、验收和账号说明均为原发布包历史内容，不代表当前电脑状态。

原始 `MANIFEST.sha256` 仅对应改造前发布包，不能用于验证本地修改后的文件。

## 原发布包说明：Sub2API 上游渠道研究与站长工具

目标：让现有订阅账号通过适合的上游协议发挥能力，同时保留 Sub2API 的客户入口、调度和计费。

当前已经接通原生 Codex WebSocket，上游模式由 Sub2API 用量记录证实。已实现候选适配器、可回滚站长操作、独立记账的协议验收和评分。能力提升尚未证实；WS、max 参数或成功返回本身不等于“满血”。

Windows 本机部署：解压发布包后双击 `Install.cmd`，详情见 [DEPLOY-WINDOWS.md](DEPLOY-WINDOWS.md)。原生 Codex CLI 也已完成真实同题对照；BPS 完整协议两次返回 403。最新路线证据见 [渠道进展_2026-10-07.md](渠道进展_2026-10-07.md)。

详细交付：[站长接入与机制研究_2026-10-07.md](站长接入与机制研究_2026-10-07.md)。当前状态：[STATUS.md](STATUS.md)。历史两渠道实测：[证实结果_2026-10-07.md](证实结果_2026-10-07.md)。

## 站长入口

双击 `焚决.cmd` 或桌面“焚决-渠道研究”仅查看状态，不发模型请求。PowerShell：

```powershell
.\scripts\Fenjue-Operator.ps1 status
.\scripts\Fenjue-Operator.ps1 restore
```

当前本机：Sub2API 0.2.13，账号 1 / 独占分组 8。HTTP 入口 `http://127.0.0.1:18080/v1`；WS 入口 `ws://127.0.0.1:18080/v1/responses`。HTTP 客户端仍走 HTTP，WS 客户端才走已启用的原生 WS 上游。没有安装额外 BPS 或 HTTP→WS 宿主插件。

快照 `runtime/sub2-ws-rollback.json` 保存改动前的三个协议字段。回滚使用原子增量更新，原先缺失字段恢复为 null（原生读取等效缺失）；保留无关运行数据。操作期间不要同时编辑同三个字段。详情见站长说明。

辅助工作台 17877 已停止；其历史代码保留用于本机协议测试，手动入口为 `scripts/Start-Fenjue.ps1`。它不是本站长交付的主入口。

## 已有组件

- `fenjue/operator.py`：状态、原生 WS 开启与恢复。
- `fenjue/discovery/alternate.py`：显式 WS 调用与最小 BPS 文本候选；禁重定向、绝对超时、严格终态，不静默回退。
- `fenjue/discovery/transport.py`：HTTP/SSE/JSON Responses，完整内存续轮状态，报告排除原始输出和加密推理。
- `scripts/trial_alternates.py`、`scripts/accept_sub2_ws.py`：已消费完的限定验收账本入口，不用于反复刷题。
- `fenjue/discovery`：冻结题目、同账号核对、固定评分和配对报告。

已从固定提交的 BPS `.s2plugin` 原始 Go 代码导出完整请求契约，并补充官方前端可核实字段；两次实际生成均返回 403。暂停同类盲试，继续原生 CLI 与官方 SIWC。后者的登录、签名验证、独立凭据保存和同 token 模型目录/Responses 适配已实现，实际接入状态见最新证据文档。

## 新研究与预算

新研究使用 `scripts/Fenjue.ps1` 调用 discovery。先复制 `local.example.json`，明确安装目录、账号、分组、模型与上限。配置仅存引用，不放 token。

1. `prepare --config CONFIG.json --out studies/NEW` 冻结题目和顺序；已有目录不覆盖。
2. `request-diff --study studies/NEW` 离线检查发出请求。
3. `inventory --study studies/NEW` 核对本机账号与独占分组。
4. `run --study studies/NEW --approve-calls N` 发起真实调用；N 必须等于冻结预算。
5. `report --study studies/NEW` 输出记录与比较。

smoke / screen / confirm 的两路径上限分别为 2 / 36 / 96 次客户端提交。已使用预算不因修改代码而重置。预算含失败、取消和完成状态未知的提交；宿主内部重试单独核查。强制退出后先确认锁对应进程已结束，保留账本再处理 stale lock。

评分来自已知答案与受限 SQL / 工具读取证据；同类变体有关联，短题全对可能只是天花板效应。协议失败独立记录。缺少健康对照时不宣称恢复原有完整能力。

## 安装与验证

本机 Python 为 `.venv/Scripts/python.exe`。依赖锁定于 `requirements.lock`；`pyproject.toml` 声明核心依赖，辅助服务需 `service` extra。

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[service]"
.\.venv\Scripts\python.exe -m unittest discover -v
.\.venv\Scripts\python.exe -m compileall -q fenjue scripts
```

`runtime/`、`studies/`、本地凭据配置均被 Git 忽略。令牌仅在内存中用于所选账号请求；不公开研究目录。`fenjue.research` 与旧网关源自用户 ZIP，来源哈希见 `SOURCE_PROVENANCE.json`；旧默认预算不代表当前调用额度。
