# Windows 运行面部署（方案 A：分层结构直接挂到运行面）

本目录是**部署件**，不属于分层代码（`hub/` / `node/`），不参与「件数 / 层间边」台账。

## 背景：为什么不能直接把分层件拷过去跑

本仓整改后，`node/l0_host/node_paths.py` 的默认值是**占位符**：

| 常量 | 值 | 语义 |
| --- | --- | --- |
| `WIN_RUNTIME_DIR` / `POSIX_RUNTIME_DIR` | `<deploy-dir>` | 未配置 ⇒ 退临时目录 |
| `WIN_NODE_EXE` / `WIN_PI_CLI_JS` / `WIN_PI_WORKDIR` | `<home>\...` | 未配置 ⇒ 返回占位符原文 |

也就是说：**源码树的默认值不是运行面真值，运行面必须显式配置**。整改前的旧扁平件
把真值硬编码在常量里（例如 Windows 运行目录直接写死），所以旧的计划任务“什么都不配”
也能跑；切到分层件后如果照抄旧实参，数据面会静默落到 `%TEMP%`。

## 部署约定

1. 运行面目录记作 `RUN_DIR`。它同时是**数据面 + 凭据面 + 日志面**（
   `*_state.json`、`*-token`、`nats-*.pass`、`*.log`、`state/`、`ctx_archive/` 都在这里），
   并且包含一个 `app/` —— 本仓 `hub/`、`node/` 的镜像。

2. 所有计划任务实参统一形如（业务参数**照搬原任务**，逐一不得改动）：

   ```
   <pythonw.exe|python.exe> "<RUN_DIR>\app\_boot.py" "<RUN_DIR>\app\node\...\xxx.py" <原业务参数...>
   ```

   原本没有工作目录的任务**不要给** `-WorkingDirectory`（见下方陷阱）；原本有的保持原值。

3. `_boot.py` 负责在启动目标脚本前注入 `AGENT_TEAM_*` 运行面真值，再以 `__main__`
   原地执行目标脚本（`runpy`，不另起子进程）。它只注入、不干预业务参数。

4. 有对应分层件的入口件才切；本仓**没有**对应件的入口保持原路径不动，避免用未搬迁的件顶替。
   批⑪ 之后，运行面上原本「本仓未收录」的三个入口件已全部归位入仓：
   `deploy/windows/a2a_launch.pyw`、`hub/l3_domain/session_ctx_archiver.py`、
   `tools/ledger_patrol.py`（依赖件 `node/l1_transport/post_group.py` 同时入仓）。

5. **真值注入清单（切运行面前必须逐项核对）**：分层件把宿主真值集中到 L0 读口后，
   下列环境变量若**不注入**，该件会拿到占位符（例如 `http://<host>:9984`）而不是真值，
   **线上行为会变** —— 这就是「归位本身不改行为，但归位新增了一条部署面义务」：

   | 环境变量 | 承接的读口 | 不注入的后果 |
   | --- | --- | --- |
   | `AGENT_TEAM_RUNTIME_DIR` | `node_paths.runtime_dir()` | 数据面静默落到 `%TEMP%` |
   | `AGENT_TEAM_CHAT_BASE` | `node_profile.chat_base_default()` | 群 / 对话台基址退成占位符 |
   | `AGENT_TEAM_TASK_BASE`（用到才需） | `node_profile.task_base_default()` | 任务服务基址退成占位符 |
   | `AGENT_TEAM_NODE_EXE` / `_PI_CLI_JS` / `_PI_WORKDIR` | `node_paths` 同名读口 | 执行器拿到未展开的 `<home>` 占位符 |

## 切换与回滚

| 步骤 | 做法 |
| --- | --- |
| 切换前 | `Export-ScheduledTask` 导出全部相关任务 XML；备份旧件与状态文件 |
| 验证 | 用 `_boot.py` 包着跑目标脚本的 `--help`（有 argparse 的件可安全冒烟）；全结构 `compileall` |
| 对账 | 逐项比较「新结构解析出的路径」与「旧扁平件解析出的路径」，**全等才切** |
| 切换 | `Set-ScheduledTask` 只换 `Action`；`Stop-ScheduledTask` → `Set` → `Start-ScheduledTask` |
| 回滚 | `Register-ScheduledTask -Xml <切换前导出的 XML> -Force`（旧件未被删除，实参恢复即回到原状态） |
| 切后验证 | 各服务的 `--help` 之外，查 `/healthz`：`nats.connected`、`last_err`、`reconnects`、心跳计数、`uptime_s` |

### 已踩到的坑（务必照做）

1. **`New-ScheduledTaskAction -WorkingDirectory ''` 会报参数校验错**
   （`ParameterArgumentValidationError`）⇒ 原本没有工作目录的任务必须**省略该参数**，
   否则该任务停在 `Ready`、服务静默下线。
2. **`Set-ScheduledTask` 失败前先 `Stop-ScheduledTask`** ⇒ 一旦中途报错，那个服务已经停了。
   切多个任务时建议支持 `-Only <TaskName>`，逐个切、逐个验。
3. **`.ps1` 一律写成纯 ASCII**：PowerShell 5.1 按 ANSI 读无 BOM 的脚本，中文（尤其全角括号）
   会导致**语法解析失败**，报错信息还是乱码。
4. **不要用 `AGENT_TEAM_*` 注入实验来“验证”装置**：Hermes terminal 的环境变量跨调用持久，
   注入会污染其后所有前台命令，造成「同一台装置单独跑绿、后台跑红」的假串扰。
5. 切换到分层件时，**先确认该件在本仓确实存在**；本仓没有的入口件保持原样。

## 部署面收录状态（批⑪ 后）

- ✅ **A2A 启动壳已入仓**：`deploy/windows/a2a_launch.pyw`（与 `_boot.py` 同源同形）。
  两者**本批并存、尚未合并**，因此 A2A 任务暂仍走原路径。
- ✅ **运行面专有入口件已全部归位**：`session_ctx_archiver.py` → `hub/l3_domain/`、
  `ledger_patrol.py` → `tools/`（依赖 `post_group.py` → `node/l1_transport/`）。
- ⚠️ 仍只在运行面、且**不宜直接入仓**的件：外部系统的一次性脚本；以及运行期文案要求持
  真值的件（须先按本仓脱敏口径把真值收进 env，再从部署面注入）。
- 📌 待办：① `a2a_launch.pyw` 与 `_boot.py` 的**能力合并**（统一成一个通用启动壳）；
  ② `session_ctx_archiver` 的水位扫描委托 `node/l2_adapter/agent_usage.py`（消除两份水位实现）。

### 部署件为何不登记进 `hacs_layers.json`

`deploy/` 不在分层扫描面内。例如 `a2a_launch.pyw` 会 import C 层 `a2a_svc`，若按代码件
登记，会被 `check_layers` 检查1 判**方向违规**；`_boot.py` 同理。**部署件的层归属由本目录承接**，
不进入「件数 / 层间边」台账。新增部署件时不要顺手登记层表。
