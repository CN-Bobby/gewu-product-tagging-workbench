# beta.6 两项 P0 修复 人工验证流程

针对 F01(反推丢弃可用响应 / 图片列不触发视觉)与 F02(对话零反馈+MCP 阻塞+主错误归因错误)的修复验证。

## 0. 结论:不需要重新安装

新代码只在源码目录 `D:\数据打标项目` 里;正在运行的 `K:\Bobbytagging\app`(安装版 `0.5.0-beta.6+2f7bf2b59975`)不包含它。验证功能只需**从源码启动**,不必打包重装。

只有要验证「安装器 / 覆盖升级」本身时才需要 `scripts/build_windows.py --candidate` 重新出包重装——那是后续候选构建阶段的事,不是本轮验证的前置。

## 1. 数据边界(务必分清)

| 位置 | 内容 | 用途 |
|---|---|---|
| `K:\Bobbytagging\Data` | 正式 310 行;F01 的失败草案与两次 `ModelAttempt` 在这里 | 第 3 层真实数据验收 |
| `K:\Bobbytagging\Data\providers` | 安装版使用的模型授权目录 | 源码运行需显式指向它 |
| `D:\数据打标项目\storage` | 旧开发库(测试10行、tankini 352 行) | 第 2 层隔离验证 |

约束不变:不动 310 行数据与 17 个人工值;不建 tag/Release;不推送 GitHub。

## 2. 前置:先提交本轮改动

自动化验收脚本 `scripts/beta_automated_acceptance.py` 要求工作区干净,并用 commit 标识被测版本。当前 12 个修改文件与 4 个新测试文件都未提交,先提交再验证。

## 3. 第 1 层:离线自动化(零成本,已通过)

```powershell
cd D:\数据打标项目
.venv\Scripts\python.exe -m pytest -q                 # 期望 192 passed
npm run build --prefix frontend                        # 期望 vite build 通过
.venv\Scripts\python.exe scripts/repository_audit.py   # 期望 214 文件、0 命中
```

每条缺陷对应的钉子(共 32 项):

| 缺陷 | 证明它的测试 |
|---|---|
| 可解析的截断响应被丢弃 | `test_p0_f01_truncation.py::test_truncated_but_valid_json_is_accepted` |
| 空内容/非法 JSON 仍须失败 | 同文件 `..._still_fails` 两项 |
| 复用已存响应不得再调模型 | 同文件 `test_recover_from_attempt_replays_stored_content_without_model_call` |
| 模型全失败仍有程序基线 | 同文件 `test_program_baseline_is_marked_unverified` |
| 原始「图片」列须等同图片资产 | `test_p0_f01_image_mapping.py::test_raw_image_column_canonicalises_to_the_image_asset_field` |
| 选项不得重名 | 同文件 `test_no_display_name_collision` |
| 回合必须有界 | `test_p0_f02_turn_limits.py` 三个 `turn_*` 用例 |
| 真实超时不得被早先的拒绝覆盖 | 同文件 `test_a_later_mcp_timeout_outranks_an_earlier_recoverable_denial` |
| 工具事件必须可见 | `test_p0_f02_progress.py::test_tool_events_rebuild_presentation_and_expose_progress` |
| 提问不得阻塞 | 同文件 `test_ask_persists_and_returns_without_blocking_the_turn` |
| 回答必须续跑 | 同文件 `test_answer_queues_exactly_one_followup_turn` |

## 4. 第 2 层:源码运行做界面检查(零成本、不碰正式数据)

```powershell
cd D:\数据打标项目
npm run build --prefix frontend
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\start.ps1 -Port 8790 -Storage D:\打标-P0测试
```

浏览器打开 `http://127.0.0.1:8790`,导入仓库根目录的 `测试10行.xlsx`(含内嵌图片列的样例),然后逐项核对:

1. 「已有标签反推/接入」→ 选一个已有标签列 → **证据列选项里不再出现两个同名项**:应为 `图片（表格内嵌图片）`、`商品主图（图片链接）`、`商品主图（已导入图片资产）`(后者带括号说明只使用已导入图片)。
2. 面板上出现「图片证据」提示,说明只有选 `商品主图（已导入图片资产）` 才会真的让模型看图。
3. 停在引导页(不点「反推定义」)即可,不要启动任务——本层不花钱。

停止:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\stop.ps1 -Port 8790 -Storage D:\打标-P0测试
```

## 5. 第 3 层:真实数据验收(需你授权,可能消耗额度)

### 5.1 先停安装版并备份

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8765/api/server/stop
Start-Sleep -Seconds 3
$ts = Get-Date -Format yyyyMMdd-HHmmss
New-Item -ItemType Directory -Force "K:\Bobbytagging\Data-backup-$ts" | Out-Null
Copy-Item K:\Bobbytagging\Data\workbench.db* "K:\Bobbytagging\Data-backup-$ts\"
```

必须停服务后再复制(`workbench.db` 现在带 4 MB WAL,运行中复制不一致)。要更保险就把整个 `K:\Bobbytagging\Data` 复制到同级新目录。

### 5.2 用新代码起在同一个网址和同一份正式数据

```powershell
cd D:\数据打标项目
$env:TAG_PROVIDER_HOME = 'K:\Bobbytagging\Data\providers'
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\start.ps1 -Storage K:\Bobbytagging\Data
```

`TAG_PROVIDER_HOME` 必须设置,否则源码运行会去读全局 `%APPDATA%\reasonix`,与安装版使用的模型授权不一致。启动后仍访问 `http://127.0.0.1:8765`,界面应显示新前端。

### 5.3 F01 零付费项:复用已保存的响应(最优先,先做这一项)

1. 「已有标签反推/接入」→ 选来源列 `上衣样式`。
2. 列表中该列显示 `失败待重试` → 点「继续」。
3. 期望失败卡显示:本轮没有产生视觉证据(视觉 0 次)、**已找到本轮保存的模型输出(3311 字,finish_reason=length)**,并出现按钮「用已保存的模型输出重建草案(不再请求模型)」。
4. 点该按钮。**期望:不再产生任何模型请求**,草案进入待审核,并出现「已复用本轮保存的模型输出」与截断横幅(缺失区块以程序默认补齐)。
5. 核对草案内容:总定义、8 条已有值规则、4 个冲突、未知策略都在;`本轮请求 文本 1 / 视觉 0`。

判定:这一步证明修复 1(解析前不再丢弃)与修复 3(零付费恢复)。

### 5.4 F01 付费项:确认图片列真的触发视觉

在上一步的草案里点「补看样本」。

1. **先看预估行**:`新增 N 件、M 张图,预计视觉请求 X 次`——修复前这里恒为 `0`,**现在必须 `X > 0`**。这一步不花钱。
2. 再勾选「补看样本,最多扩到60件」并确认。期望任务详情里 `视觉` 次数 `> 0`,且不再出现"310 件有图却 0 次视觉"。

判定:这一步证明修复 2(原始图片列不再静默禁用视觉)。会消耗视觉额度。

### 5.5 F02 付费项:新对话的可见进度与提问不阻塞

1. 新建对话,输入:`判断上衣样式，我人工打了一些标签可以结合图片做参考`。
2. **数秒内**必须出现进度行:`已运行 … · 最近活动 …前 · 工具 N 次`.
3. 若模型提出业务问题:问题卡应出现,且**不再卡满 5 分钟**;回答后自动从下一步继续(回合已结束也能答)。
4. 点「停止回复」应在数秒内结束,消息与已完成工具结果保留,数据集不再 busy。
5. 若出现工具超时,错误码应为 `tool_timeout`;不应再出现 `Reasonix 尝试调用工作台未开放的工具“run_skill”` 作为主错误。

判定:这一步证明 F02 的四项修复(进度、提问桥接、分层超时、错误归因)。

### 5.6 恢复回原状

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\stop.ps1 -Storage K:\Bobbytagging\Data
```

然后从开始菜单/桌面「格物工作台」重新打开安装版。若需回滚数据(先停服务):

```powershell
Copy-Item "K:\Bobbytagging\Data-backup-<时间戳>\workbench.db*" K:\Bobbytagging\Data\ -Force
```

## 6. 已知的预期变化(不是故障)

- 提示词调整使 `instruction_hash` 变化,现有原生会话会在下次对话时被停用并重建,并注入受控上下文;网页消息与工作表成果不受影响。
- 源码运行会在 `K:\Bobbytagging\Data\datasets\<数据集>\reasonix` 下重写派生配置,这是设计行为。
- 数据库 schema 仍为 8,不发生迁移;因此回滚只需换回程序与备份数据库。

## 7. 反馈记录格式

问题反馈请同时记录:被测源码 commit、发生时间、工作表、对话名、操作顺序、界面技术详情。不要提交完整 API Key。

在此之前请使用「代码已修复待构建 / 候选已构建待真实验收」等精确状态,不使用"已经好了"。
