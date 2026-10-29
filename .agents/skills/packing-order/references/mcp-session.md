# MCP 持续会话与清单调度

完整 packing-order 流程使用 `scripts/mcp_session.py` 维持一个标准 `mcp.ClientSession` 至工单和领料阶段结束，所有业务调用仍是 `call_tool`，不在阶段之间反复启动或登录。少量独立查询可用当前可调用的MCP工具。固定业务助手直接运行；正常执行无需读取Python实现。

从当前项目的 `.codex/config.toml` 一次核实E10服务器的command、args和cwd，以当前配置实际路径为准；项目中的同名集成镜像不一定是运行入口，不再猜解释器或扫描其他目录。同轮复用已核实路径，跨电脑按目标电脑实际配置。不要扫描或打印凭据文件。匹配助手使用 `load_workspace_dependencies` 提供的Python，与E10连接解释器用途不同。

本机凭据使用Windows当前用户DPAPI加密。持续会话须运行于保存凭据的EDY用户环境；沙箱账号不能解密。登录组件异常时按[身份恢复](mcp-session-recovery.md#身份与登录组件)处理。

启动时会自动完成本地 `runtime_preflight.py` 检查并保存 `runtime-preflight.json`，失败直接退出，不启动MCP或登录。检查解释器是否为该集成 `.venv/Scripts/python.exe`、MCP SDK、客户端依赖、脚本语法及真实中文表头/排除行契约。Windows身份检查只读凭据文件所有者元数据，不读取/解密凭据；匹配不代表认证成功。需要单独排查时在同一启动命令加 `--preflight`，只做本地检查；正常启动无需先重复跑一次。表格XLSX依赖使用工作区捆绑解释器，不要求E10解释器额外安装表格包。预检/本地回归失败先完成修复，再恢复业务阶段。

在终端持久会话中启动（使用实际绝对路径）：

```text
E10连接环境的python -u 本技能/scripts/mcp_session.py --server 实际E10目录/server.py --work-dir 本轮工作目录
```

在Windows终端使用持续输入（exec工具需分配TTY），取得session ID后通过 `write_stdin` 发送短JSON入口。每个阶段的完整命令先保存在本轮 `commands/阶段.json`，再发送：

```json
{"command_file":"commands/阶段.json"}
```

文件内容为本页下文各阶段的单个JSON对象，不能放命令数组或嵌套command_file。可选 `command_sha256` 绑定命令文件；路径限定本轮目录，助手读取实际字节并在完整回执及阶段日志记录路径和SHA256。长只读清单也放文件，避免终端行长度截断。旧单行命令保持兼容，结束仍可直接发送 `{"exit":true}`。请求文件和结果路径相对于本轮目录。先显式登录，确认成功后继续；助手不会自行登录或自动重试。

初始化后完整读取一次当前MCP工具元数据并缓存 `session-output-会话标识/capabilities.json`，核对真实inputSchema；不登录、不调用业务工具、不打印全目录。同一会话复用该缓存；目录缺页、重复工具或不支持的外部schema引用会停止。`--discover-tools`仅另存简短工具名清单，复用缓存。每次调用前核对工具存在及参数，已知字段拼错、类型错误、未知job/phase、缺输入、已有回执或越界路径均在调用前拒绝。此能力检查不替代授权、BOM、版本及防重复业务门槛。

命令可先用E10配置解释器离线检查，不启动MCP：

```text
python scripts/session_commands.py --base 本轮工作目录 --command-file commands/阶段.json --capabilities 本轮工作目录/session-output-实际会话/capabilities.json
```

元数据尚未生成时可省略 `--capabilities`，结果明确标注未检查在线工具能力；正式会话始终按实际缓存验证。E10解释器使用MCP依赖自带的jsonschema；不要求匹配用捆绑Python额外安装。

命令结果保留原阶段状态、计数、`prepared_sha256`/`pending_sha256`、路径及错误，并增加 `event=result`、`full_result`、`full_result_sha256`。完整原始命令结果按序独占保存到 `session-output-会话标识/000001.json` 等文件；异常长正文和大清单只在文件中，摘要不参与业务校验或授权。`in_flight`、unknown、失败仍立即停止依赖写入。原业务回执、检查点、started标记及计时日志保持原用途，不能用输出文件替代。

全部进度保存在同目录progress.jsonl；终端节流、等待及180秒暂停统一按[执行约定](performance.md)。正常只等待当前终端，不逐单轮询文件。复用空闲子代理用followup_task，send_message只补充运行中的任务。

普通调用的请求文件内容是 `{"name":"e10_工具名","arguments":{...}}`，发送：

```json
{"request":"请求文件.json","output":"结果文件.json"}
```

已知参数的独立主数据、工单/领料分页查询可以用 `job=read_sequence`，最多50项；依赖上一页游标的分页继续用collect，不预猜页号。每项引用本轮请求文件及独立结果文件。助手先核对全部只读工具、实际参数schema、输入和输出，任一不合格则整批零调用；通过后串行执行，任何失败立即停止。登录、准备、丢弃、保存、审核均不能放入只读批次。下例为 `commands/read-master.json` 文件内容：

```json
{"job":"read_sequence","steps":[{"request":"doc-request.json","output":"doc.json"},{"request":"applicant-request.json","output":"applicant.json"}]}
```

正式创建/审核仍在完整准备或待审核清单核对之后单独调度；不把未来依赖的写入预先排队。授权已明确且程序门槛通过，就进入下一阶段，不反复展开成功行或重新建立同义证据索引。

read_sequence每项在原工具名、output和ok之外附带白名单summary，列表仅给数量，完整结果原样保存在output。优先一次读取批次摘要及异常；只有新业务门槛需要而原校验未覆盖时再定点读取原始结果，不逐个打开所有成功文件，也不用summary授权写入。

正式请求须同时指向本轮完整清单和已存在的用户授权范围，保留清单哈希及实际数量。用户已明确授权完整流程或该集合的生成/审核时沿用授权；后续收集清单不收窄先前授权，也不重复提问。缺少的授权在清单可审阅后一次提出。自动审批若仍拒绝，按其原因停止对应动作，不通过改接口、改目录或重复发送绕过。

完整工单阶段使用[双分支控制器](workorder-branches.md)，其恢复及审核命令按该页；本会话不另混用订单或手工助手提前审核。

完整分页收集可发送：

```json
{
  "job": "collect",
  "filters": {"applicant_name":"本次申请人员完整姓名","approval_status":"N"},
  "include_detail": false,
  "pages": "待审核分页",
  "output": "待审核完整清单.json"
}
```

只有完成全部分页，助手才写出 `ok=true, complete=true, has_next_page=false` 的完整清单。明细收集使用本次筛选条件并改为 `include_detail=true`。每轮用新目录，不能把不同查询的分页混在一起。

**仅在本次用户已授权审核该集合、且模型核对清单之后**，发送清单调度命令：

```json
{
  "job": "approve",
  "pending": "待审核完整清单.json",
  "applicant_name": "本次申请人员完整姓名",
  "applicant_id": "本次唯一员工ID",
  "checkpoint": "审核检查点.json",
  "receipts": "逐单审核回执"
}
```

助手按清单顺序准备并提交，保存每单回执、当前进行中的操作 ID、完成/跳过项，定期打印进度；申请人变化、未解决的提交状态或错误会停止。中断后按[状态核实](mcp-session-recovery.md#认证失效与中断)处理。

领料source-request/source的范围、完整列表及状态门槛统一见[后续阶段](downstream-stages.md#4-完整领料源)，本页仅承载collect/approve传输，不另定义生成或回填范围。

结束前用普通请求显式 `e10_logout`，再发送 `{"exit":true}`。这只结束本助手的会话，不退出用户客户端。

## 调用计时与认证恢复

`mcp-calls.jsonl`按会话与序号记录工具名、UTC开始/返回时间、实际SDK等待秒数及结果状态；不记录参数、凭据或业务明细。手工来源助手在阶段目录的`calls.jsonl`另记回执路径，便于对应耗时。日志覆盖读取、准备、保存和审核，文件修改时间只作辅助时间线。

`workflow-timing.jsonl`记录阶段命令耗时及since_previous_command_seconds；progress.jsonl含observed_utc。日志重叠和度量边界按[执行约定](performance.md#度量边界)，不以文件mtime估计接口耗时。

需要采集正常授权任务的性能样本时，在同一启动命令加 `--profile-local`。默认不生成附加性能日志；开启后仅在本轮 `local-performance.jsonl` 追加数字汇总，不输出大日志。覆盖范围、指标口径和缓存边界见[可选性能记录](performance.md#可选性能记录)。不为采样重新执行业务，不在每个命令间重启会话，也不要求 Agent 手工计算或比较 SHA256。

认证失效与中断按[异常恢复](mcp-session-recovery.md)处理，正常同轮复用已核实路径和完整快照。
