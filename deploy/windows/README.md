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

4. 有对应分层件的入口件才切；本仓**没有**对应件的入口（例如若干采集/运维脚本、
   A2A 启动壳）保持原路径不动，避免用未搬迁的件顶替。

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

## 已知的部署面缺口（待补）

- A2A 服务的启动壳（把 `stdout/stderr` 落到日志 + 等尾网地址就绪再 `import`）目前只在运行面
  存在，仓内没有等价件；补齐后 A2A 任务才可一并切到分层结构。
- 运行面上还有若干本仓未收录的入口脚本（采集、巡检、任务系统等），它们仍指向旧位置。
