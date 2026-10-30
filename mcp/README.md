# E10 MCP 换机安装包

先读本页即可安装，无需重新研究接口或复制旧电脑配置。包含当前 Python MCP、C# 适配器、PowerShell 桥接、锁定依赖、五份包装参考手册及配置模板；来源为 2026-10-30 当前生效的共享安装。旧 C# 独立账号服务未作为第二套 MCP 打包。

## 新电脑首次使用

1. 克隆或下载整个私有仓库到普通本地目录；安装 Windows、Python 3.12 和兼容的鼎捷 E10 客户端。客户端供应商 DLL 由正常 E10 安装提供，本仓库不分发。确认网络能访问本公司的 E10 服务器。
2. 双击 `01-install.cmd`。脚本检查客户端组件、创建 Python 环境、安装锁定依赖，并按当前仓库路径生成本机 `connection.json` 及项目 `.codex/config.toml`。默认客户端目录为 `%LOCALAPPDATA%/Digiwin/E10/Client`。如果路径不同，在仓库终端运行：

   ```powershell
   py -3.12 mcp/install.py --client-directory "D:/E10/Client"
   ```

   默认服务器为 `http://192.168.0.2:8002/WCFGeneralService`，账套 `DemoCompany`（大寰公司）。其他环境用 `--server-url`、`--account-set-id`、`--account-set-name` 指定。已有不同配置时停止并要求手动合并，不覆盖其他 MCP 设置。运行脚本不会修改当前电脑原共享安装。
3. 双击 `02-configure-account.cmd`，仅在本机窗口输入 E10 账号密码并保存。凭据由当前 Windows 用户 DPAPI 加密，不上传，也不能跨电脑直接复用。
4. 在 Codex 打开并信任此仓库，重开会话使项目 MCP 配置生效。先确认 `dingjie-e10-account` 工具和 `e10_read_manual(manual_id="integration-guide")` 可读；明确登录后通过 `e10_current_user` 核对账号。不可通过开单、审核或生成领料测试安装。
5. 在新电脑独立连接官方钉钉电子表格 MCP 并授权对应文档。官方托管插件及其授权无法通过本地配置文件复制。

## 日常更新与检查

在另一台电脑 `git pull` 后，重新运行 `01-install.cmd` 更新依赖；已有同内容配置可重复使用。文件和依赖检查：

```powershell
py -3.12 mcp/install.py --check
```

自定义客户端/服务器参数须与首次安装一致。该检查不登录 ERP；首次安装的跨电脑实际连通性仍需在目标电脑验证。本包已在源电脑验证 MCP 初始化、工具清单和离线手册，不进行正式业务写入。

`e10/接口清单.md` 由本次源码生成，仅帮助定位工具；实际参数以连接后的 schema 为准。五份参考手册保留原块号和截图；已排除旧项目交接、原始 Office 快照及业务回执。历史示例不能当作当前数据或业务授权。

## 同步边界

Git 保存 `mcp/` 内源码、模板、手册和安装步骤；`.venv/`、`.codex/`、`connection.json`、`credentials.dpapi`、`receipts/` 和账号窗口状态均排除。账号、供应商客户端、业务记录及未决操作状态单独交接。同一批业务只由一台电脑操作；换机不能重放未决业务。

MCP 配置结构依据 [OpenAI 官方 MCP 文档](https://learn.chatgpt.com/docs/extend/mcp)。
