# Cockpit 出站接管：核查与待实施边界

2026-10-09。v3 已部署并启用。下方核查记录保留为设计依据。

## 部署结果

- 已获得用户对备份、替换 `cockpit-cliproxy.exe`、重启 Cockpit 的明确授权。
- 补丁源码：`cockpit-patch/`；可复现覆盖脚本：`scripts/prepare_cockpit_patch.py`；构建入口：`scripts/Build-CockpitPatch.ps1`。
- 官方源固定到下述提交，使用 SHA256 校验的官方 Go 1.26.9 Windows 工具链构建。部署记录与原版哈希位于忽略目录 `runtime/guard/deployment.local.json`，原版程序有独立备份。
- 开关位于安装目录的受限权限子目录 `fenjue-guard/control.local.json`，不在 Cockpit 生成配置内。这里只保存本地保护层密钥与账号引用，不保存 OAuth token；OAuth 仅从 Cockpit 内存经固定回环地址传给焚决，再核对选中身份，不替换为另一个账号。
- Cockpit 原有鉴权、模型访问限制和账号作用域检查继续执行；固定账号由其账号管理器选择。转交的是策略重写前的原始请求，若检测到模型重映射则拒绝。出站/返回不再经过旧执行器的请求转换和推理项清理。用量通过 Cockpit 的 usage 机制回报。
- 正常关闭先将开关置为关闭，再等待在途请求归零后停服务。同次开机内异常终止保持失败关闭；开关绑定 Windows 启动时间，电脑重启后旧开关失效、默认不接管。没有添加开机自启。启动身份读取失败时明确拒绝，不猜测系统状态。

## 本轮验证

- Cockpit sidecar 全套本地测试通过（含新增开关、原文转交、故障拒绝、状态接口测试）。
- 焚决相关 38 项测试通过。全 Python 146 项：144 通过、1 跳过、1 原有 SIWC Windows ACL 测试失败。
- 安装后真实开/关/再开验证通过；开关前后 Codex 配置字节相同。当前入口为 `http://127.0.0.1:64492/v1`。
- 从真实 Cockpit 发起非生成身份探测，焚决确认收到固定账号所选凭据；没有为验收追加生成请求。
- 开启状态下重启 Cockpit，转发 PID 改变，接管仍启用，身份探测再次通过，Codex 仍连接 Cockpit。
- “Windows 新一次开机旧开关失效”由启动身份替换的本地测试覆盖；没有为了测试重启用户电脑。
- 最后一次部署重启前状态有 3 条在途业务请求，可能被中断；已明确告知用户重试受影响任务，并为后续重复安装/恢复加入非空在途请求拒绝部署检查。检查与重启之间仍存在短暂竞争窗口，建议空闲时维护。
- 实际业务账本已观察到 `cockpit_outbound / gpt-6-astra / high / completed`。这不是新增合成验收调用，也不能代替完整工具/压缩真实验收。
- 新链路的工具、加密状态及压缩保留由本地测试覆盖；此前 v2 的 8 次真实协议验收属于旧链路，不冒充 v3 完整真实验收。质量提升仍未证实。

## 使用和边界

`焚决.cmd` 开启；`Stop-Guard.cmd` 关闭；`Status-Guard.cmd` 看真实状态；`Restore-Cockpit.cmd` 校验备份并恢复原版转发程序（重启 Cockpit）。首次迁移请重启 Codex 并新开任务。

只保护绑定接入密钥的 `gpt-*` / `codex*` HTTP Responses 与压缩；WebSocket 拒绝，其他模型和独立语音/图片接口保留 Cockpit 原处理，不宣称受保护。显式 medium/high 原样保留，只有缺省 effort 才用 max。

Cockpit 更新会覆盖自编译程序，不能保证更新后仍有补丁。状态工具会检测哈希/扩展接口，发现不匹配应停止声称已保护；不会自动覆盖新版程序。会话跨越开关时账号策略可能变化，建议新开任务。

## 实施前核查记录（历史）

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

## 核查时实际状态（部署前，历史）

核查时原 v2.0 前置服务 `/health` 为 200，Codex 活跃 provider 的入口仍为 `http://127.0.0.1:17878/v1`。这是旧链路，不是新的 Cockpit 出站接管。新链路尚未启用。
