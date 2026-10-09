# Cockpit 出站接管：核查与待实施边界

2026-10-09。此文档不是已启用声明。

## 用户要求

Codex 始终连接 Cockpit。开启焚决后，Cockpit 选定 GPT OAuth 账号的出站经过焚决；正常关闭焚决时先解除接管，再停止保护层。不改 Codex 入口来实现开关，不后台争抢 Codex 配置。

## 已完成

- Git 本地源码基线：`dd57281`；排除 runtime、虚拟环境、安装轮包及本地凭据。未推送远程。
- 对照已安装 Cockpit Tools 1.3.66，核查官方 tag `v1.3.66`，commit `5273335ef9a96611336c4b927759c125a0f092d7`。研究副本在忽略目录 `runtime/cockpit-source`，不是部署产物。
- 本次未修改 Cockpit 运行配置、OAuth 凭据、安装目录或 Codex 入口；没有提交真实生成请求。

## 源码证据

官方源码仓库：https://github.com/jlcodes99/cockpit-tools/tree/v1.3.66

- `sidecars/cockpit-cliproxy/runtime_auth.go` 的 `readManifestCodexTokenAuth` 为 OAuth 构造固定的属性集合，没有从账号配置映射 `base_url`。
- `third_party/CLIProxyAPI/internal/runtime/executor/codex_executor_auth.go` 的 `codexCreds` 从 Attributes 读取 `base_url`；execute/stream 在没有该属性时使用官方 Codex 地址。API Key 账号的可配地址不等于 OAuth 账号有同一入口。
- `src-tauri/src/modules/codex_local_access_sidecar_config.rs` 从已知字段重建 config/manifest 并写盘，没有保留任意插件配置的合并路径。
- `third_party/CLIProxyAPI/sdk/cliproxy/cockpit_runtime.go` 的 `StartRuntime` 注册基础执行器，没有初始化/应用动态插件；`RebindRuntimeExecutors` 同样不请求插件执行器注册。不能仅因底层 SDK 有插件目录就宣称已安装的 Cockpit 支持该接入。
- `sidecar_binary_candidates` 优先寻找 Cockpit 安装目录的转发程序，没有读到外部可执行文件覆盖环境变量。

因此，仅改临时 sidecar 配置或普通 HTTPS CONNECT 代理不能可靠实现请求内容保护与持久的开关语义。

## 待用户确认的部署变更

推荐在对应版本的 sidecar 源码中增加专用出站接管入口，编译、隔离测试后，一次性备份并替换安装目录中的 `cockpit-cliproxy.exe`。保持 Cockpit 主程序、登录与刷新职责不变。不使用 TLS 中间人，不安装根证书，不关闭 TLS 验证。

这涉及第三方已安装程序替换和 Cockpit API 服务重启，尚未执行。不是已经完成的改造。

实施必须包括：

1. 独立于 Cockpit 生成配置的可原子切换控制状态；启动就绪后接管，停止时先解除并确认，再停服务。
2. 原版程序备份、哈希校验、还原入口；升级发现不匹配时明确报失效，不能显示仍在保护。
3. 选定账号不替换、重试/回退有边界、请求/响应不偷偷改模型；保护层不可用时明确报错，不静默绕过。异常崩溃与正常关闭必须分别处理。
4. 测试固定账号、工具续接、完整压缩输出、关闭恢复、并发开关与 Cockpit 重启。不能用旧前置网关验收记录证明新出站链路已验证。
5. 出站处无法自动修复此前已经丢失的请求字段；需额外检查 Cockpit 入站到出站的变换，不能承诺“保证不降智”。

## 当前实际状态

核查时原 v2.0 前置服务 `/health` 为 200，Codex 活跃 provider 的入口仍为 `http://127.0.0.1:17878/v1`。这是旧链路，不是新的 Cockpit 出站接管。新链路尚未启用。
