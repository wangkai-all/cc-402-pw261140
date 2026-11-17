# 分区数据加工—留存—回收系统

分区留存期限缩短后，旧数据可能仍被**未完成的加工任务**和**已封存报告**引用。
本系统让策略服务、加工执行器、回收器协作，基于**可追溯的引用台账 + 虚拟时钟
到期规则**决定回收，而不是按文件日期删除；失联任务不会永久阻塞清理，进程恢复
时必须重新确认读取权。所有回收都是**真实文件 unlink**，不以改数据库状态代替。

## 运行

```bash
# 启动（纯 Python 标准库，无第三方依赖）
python3 scripts/run_server.py --reset          # http://127.0.0.1:8000
# 可选：--port、--heartbeat-timeout（失联判定秒数，默认 15）、--var

# 端到端测试（12 个用例，覆盖全部关键机制）
python3 -m unittest discover -s tests -v
```

## 核心设计

### 双时钟
- **虚拟业务时钟**（网页可快进）：留存到期、grant 过期全部用它，便于演示完整生命周期。
- **真实墙钟**：任务心跳与失联判定。"进程是否活着"是物理事实，不能被业务时钟伪造。

### 引用授权台账 `grants`（只追加状态流转）
| 类型 | 含义 | 过期/释放方式 |
|---|---|---|
| `TASK_LEASE` | 加工任务对分区原件的读取租约 | 任务完成 RELEASED；心跳超时失联 REVOKED |
| `REPORT_HOLD` | HOLD 模式封存报告护住原件（延迟回收） | 虚拟时间到期 EXPIRED；续用则旧券 RENEWED + 发新券 |
| `COPY_BINDING` | COPY 模式报告绑定独立副本 | 副本有独立过期点，原件不被它保护 |

状态链 `ACTIVE → RELEASED / REVOKED / EXPIRED / RENEWED` 全程留痕，
`renewed_by_grant` 串起续用链；`events` 表 append-only 记录每个动作。

### 回收判定（`retention/reaper.py`，从不看文件 mtime）
1. 先把虚拟时间到期的 ACTIVE grant 流转为 EXPIRED；
2. 副本：无有效 `COPY_BINDING` → 可回收；
3. 原件：留存期（按**分区创建时适用的策略版本**计算）未到 → 保留；
   已到期但仍有有效 `TASK_LEASE`/`REPORT_HOLD` → 保留并给出原因；
   两者都无 → 可回收。
4. 执行回收：锁内二次确认无新 grant 抢入（防 TOCTOU）→ `os.remove` 真实文件
   → 断言文件消失 → 按 `stat` 真实字节记账。

### 失联与恢复
- worker 周期心跳；supervisor 按真实墙钟扫描，超时（默认 15s）判 `LOST`，
  令牌作废、租约 `REVOKED` —— 阻塞随即解除，网页可手动"触发失联"模拟崩溃。
- 恢复（resume）不相信旧状态，逐分区重新鉴权：
  文件存在？进程有 `os.R_OK`？在任务声明的输入清单内？
  任一不过 → 任务 `FAILED` + `RESUME_ACCESS_DENIED` 审计；
  全过 → 换新租约令牌（旧 worker 凭旧令牌自动退出），断点续跑。

### HOLD vs COPY
- **HOLD（延迟回收）**：不复制，报告持有 `REPORT_HOLD`，原件在续用到期前受保护；
- **COPY（必要副本）**：真实复制到 `var/data/copies/`（sha256 校验一致），
  原件可立刻进入回收流程，副本作为独立存储单元单独过期。

## 网页控制台

`http://127.0.0.1:8000` 六个页签：
1. **可清理范围/回收**：dry-run 预览每个单元的保留原因、可释放字节；一键真实回收；
2. **分区与留存原因**：策略版本、虚拟到期点、物理文件是否在盘、被谁引用；
3. **加工任务**：进度、心跳年龄，按钮触发失联 / 恢复（显示重新鉴权结果）；
4. **封存报告**：HOLD/COPY 模式、grant 过期点、续用操作；
5. **引用授权台账**：全部 grant 状态流转与续用链；
6. **审计事件**：append-only 动作流水。

顶部 KPI 显示在盘占用与**累计真实释放字节**。

## 目录

```
retention/   核心服务（clock/db/events/storage/policy/executor/reports/reaper/app/http_api）
web/         原生 HTML/CSS/JS 控制台（无构建、无外部依赖）
tests/       12 个端到端 unittest
scripts/     run_server.py 入口
var/         运行期产物（gitignore）：meta.db 元数据 + data/ 真实文件
```
