# 焚决 Windows 部署包

> 本目录已于 2026-10-09 改为 Cockpit 默认入口。当前使用请看 [COCKPIT.md](COCKPIT.md)，无需 Sub2API。以下为原包部署记录；原安装模式需显式传入 `-Backend Sub2API`。

适配本次已安装的 Windows Sub2API 0.2.13：API 端口 18080，PostgreSQL 端口 15432，安装目录 `%LOCALAPPDATA%\Sub2API`。默认账号 1、独占分组 8。安装保留现有 Sub2API 数据与设置。

## 安装和使用

1. 将 ZIP 完整解压到一个可写目录，例如 `D:\Fenjue`；请勿直接在 ZIP 中运行。
2. 双击 `Install.cmd`。脚本使用本机 Python 3.12 x64，并从包内 `wheelhouse` 离线安装全部项目依赖。当前机器会自动发现 Codex 附带的 Python。首次生成 `screen.proposal.json`，重复运行保留该文件。
3. 双击 `焚决.cmd` 查看 Sub2API 账号和协议设置。研究工作台入口是 `Start-Workbench.cmd`，页面选择解压目录内 `runtime/gateway-key.local.json` 连接；退出用 `Stop-Workbench.cmd`。

安装脚本只创建项目环境及配置；真实模型请求由你在工作台或研究命令中发起。Sub2API 本身需要已经运行。源码包附有全部 Python 源码、测试、协议研究记录和依赖 wheel，Python 解释器及 Sub2API 的程序和数据库沿用本机安装。

自定义路径或账号：

```powershell
.\scripts\Install-Fenjue.ps1 -PythonExe 'C:\Python312\python.exe' -Sub2Install 'D:\Sub2API' -AccountId 1 -GroupId 8
.\scripts\Fenjue-Operator.ps1 status
```

已有 `screen.proposal.json` 时，编辑该文件调整绑定。当前适配器读取所选安装的 `private-settings.json` 和内置 psql，检查独占 OAuth 分组。自建 Docker/Linux Sub2API 的配置布局不同，当前 Windows 包针对上述安装布局验收。

## 可用入口及状态

| 入口 | 当前作用 |
|---|---|
| `焚决.cmd` | 站长状态；`restore` 可恢复本目录保存的协议快照 |
| `Start-Workbench.cmd` | 本机 HTTP / Sub2API 两路 Responses 辅助工作台，最多 10 个在途请求 |
| `fenjue/discovery/alternate.py` | Codex WebSocket 调用；Sub2API 原生 WS 已有真实验收 |
| `fenjue/discovery/native_codex.py` | 隔离官方 Codex 客户端，已完成单题同账号调用与 HTTP 对照 |
| `scripts/login_siwc_local.py` | 官方 SIWC 订阅授权；新凭据独立存储，后续用 `accept_siwc --mode inventory` 验证模型目录 |
| `protocol/bps_contract_export_test.go` | 从固定版本的公开 BPS 插件导出离线协议契约；本账号完整请求得到 403 |

新包无原机器运行账本和回滚快照。`enable-ws` 要求本机新鲜的两次 WS 验收证据；首次研究请按 README 的冻结预算流程进行。已有机器的快照仍在原工程 `runtime/sub2-ws-rollback.json`，请在原工程执行恢复，或自行保管后移入新目录。请勿删除既有研究账本来重复同一轮验收。

原生客户端可通过 `FENJUE_CODEX_EXE` 指定可执行文件，当前实测版本为 0.160.1。试验通过只证明调用路径可用。BPS 失败、SIWC 待授权、原生客户端单题成功均按实际状态呈现；质量优势及长期“满血”仍需独立证据。

用现有 Sub2API 保存的 JSON 凭据发起原生客户端任务：将任务保存为 UTF-8 的 `task.txt`，在解压目录执行下方命令。每次执行会产生一次客户端提交及实际额度消耗，CLI 内部重试次数取决于其实现。此入口为文本研究通道，关闭了本地工具执行。

```powershell
.\.venv\Scripts\python.exe -X utf8 -m fenjue.native --input task.txt --effort high
```

当前 JSON 具备 Codex OAuth 凭据，适用于已接通的三条路线。SIWC 需要另外完成官方登录以取得 direct scope；JSON 中已有的 refresh token 仍由 Sub2API 管理，安装不会将其轮换为另一应用的 token。

## 凭据与维护

部署包按白名单打包，包含 `MANIFEST.sha256`。未打包账号 token、密码、私有设置、运行日志、研究账本和 `.venv`。运行时生成的 `runtime/` 保留在你的机器上，勿分享该目录。Python 第三方依赖的许可证保留在各 wheel 内。

离线验证：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -v
.\.venv\Scripts\python.exe -m pip check
```

服务监听 `127.0.0.1:17877`，用于本机辅助研究；中转站客户流量继续使用 Sub2API 原入口。工作台流式输出当前为完成后缓冲发送。完整实测记录见 `渠道进展_2026-10-07.md`。
