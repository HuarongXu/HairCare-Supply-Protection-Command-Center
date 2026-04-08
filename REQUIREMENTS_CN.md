# MR Upload Tool - 需求与部署说明（中文）

## 1）项目目的
本项目提供：
- MatRes / HC IDP 数据处理流水线（Pipeline）
- 交互式 Dash 看板（Demand Assumption / Supply Protection / Project Details）

## 2）运行环境要求
- 操作系统：Windows 10/11 或 Windows Server（推荐 VM 部署）
- Python：3.11+（当前已验证 3.13）
- 网络：如需分享链接，需内网可访问
- 浏览器：推荐 Edge / Chrome

## 3）Python 依赖
使用 `requirements.txt` 安装：

```powershell
pip install -r requirements.txt
```

主要依赖：
- dash
- pandas
- plotly
- openpyxl
- waitress（用于 VM 稳定托管）

## 4）项目关键路径
- Dashboard：`dashboards/matres_app.py`
- Pipeline：`scripts/matres_pipeline.py`
- 配置：`config/config.json`
- 处理结果：`data/processed/`

## 5）本地开发运行
### 5.1 运行 Pipeline
```powershell
& ".\.venv\Scripts\python.exe" scripts\matres_pipeline.py
```

### 5.2 运行 Dashboard（调试模式）
```powershell
& ".\.venv\Scripts\python.exe" dashboards\matres_app.py
```

## 6）VM 部署（推荐共享方式）
> 目标：通过邮件发 URL，保留完整交互功能。

### 6.1 复制项目到 VM
复制完整项目目录（至少包含 `config/`, `dashboards/`, `scripts/`, `data/`）。

### 6.2 在 VM 创建并激活虚拟环境
```powershell
python -m venv .venv
& ".\.venv\Scripts\Activate.ps1"
pip install -r requirements.txt
```

### 6.3 在 VM 运行 Pipeline
```powershell
& ".\.venv\Scripts\python.exe" scripts\matres_pipeline.py
```

### 6.4 启动 Dashboard（监听所有网卡）
```powershell
& ".\.venv\Scripts\python.exe" -c "from dashboards.matres_app import app; app.run(host='0.0.0.0', port=8050, debug=False)"
```

### 6.5 开放防火墙端口（一次）
（管理员权限）
```powershell
netsh advfirewall firewall add rule name="Dash8050" dir=in action=allow protocol=TCP localport=8050
```

### 6.6 访问地址
- VM 本机：`http://127.0.0.1:8050`
- 局域网：`http://<VM_IP>:8050`

## 7）生产稳定启动（Waitress）
```powershell
& ".\.venv\Scripts\waitress-serve.exe" --listen=0.0.0.0:8050 dashboards.matres_app:app.server
```

## 8）常见问题排查
### 8.1 `127.0.0.1 refused to connect`
- 应用进程未启动或已退出
- Python/venv 路径错误
- 端口未监听（`netstat -ano | findstr :8050`）

### 8.2 `python.exe not recognized`
- VM 上项目未复制完整或路径不一致
- 在项目根目录使用相对命令：
```powershell
& ".\.venv\Scripts\python.exe" ...
```

### 8.3 VM IP 可访问但 127.0.0.1 不可访问
- 浏览器代理设置影响 localhost
- 尝试 `http://localhost:8050` 并在代理中放行 localhost/127.0.0.1

## 9）建议运维方式
- 用 Windows 任务计划定时跑 `scripts/matres_pipeline.py`
- Dashboard 在 VM 常驻（任务计划或 NSSM）
- 邮件仅发 URL，不发送“离线 HTML 完整交互”预期

## 10）业务逻辑与计算口径

### 10.1 数据源
- MatRes 主数据：来自 `config/config.json` 指定工作簿
- HC IDP 报表：项目根目录最新 `HC IDP HANA TD Report*.xls*`
  - `Monthly`：用于月份数据
  - `Weekly(TP)`：用于当前月覆盖（ER -> LBE）
- 历史基线：`Historical Shipment Data_FY2425.xlsx`（`Sheet1`）

### 10.2 Pipeline 产出文件（`data/processed`）
- `monthly_msu_by_item_text.csv`
- `monthly_msu_by_requester_item.csv`
- `monthly_msu_by_level1.csv`
- `pde_alerts.csv`
- `matres_request_details.csv`
- `level1_unmapped_materials.csv`
- `hc_idp_monthly_summary.csv`

### 10.3 角色映射规则
- 映射文件：`config/requester_roles.json`
- Email 预处理包含常见 typo 修复（如 `@pg,com -> @pg.com`）
- 未匹配角色归为 `Others`

### 10.4 Supply Protection（Level1）映射规则
- 来自 Level1 映射工作簿（配置项控制）
- 若映射缺失：
  - `Item Text = RM Material`：强制归到 `Base`
  - 其他类型：保留 `未映射`
- UI 中 `未映射` 若显示值近似为 0，会自动隐藏

### 10.5 Demand LBE 逻辑
- UI 维度显示名：`Prod Line`（Base / Promotion / Total）
- 时间窗口：当前季度 + 后续 2 个季度（共 9 个月）
- 当前月取数：`Weekly(TP)` 的 ER->LBE，且 SU 转 MSU（/1000）
- 非当前月取数：`Monthly`（同样标准化为 MSU）

### 10.11 Demand Data 时间窗口规则
- `TD Version Monthly Comparison`：9 个月（当前季度 + 后续 2 个季度）。
- `Level2 GAP Details` / `GAP Difference Details`：同样按 9 个月窗口展示。

### 10.12 Production Vol 筛选与月份规则
- Production Vol 基础筛选条件：
  - `Categories / Members = 2.0Production/Receipts`
  - `Plant` 非空
  - `Material` 非空
  - `MRP Elements` 仅保留 `2.1Planned Orders` 与 `2.2Process Orders`
- 业务口径上剔除 `Other`：因 `Other` 包含 QM，而 QM 已在 MTD 中统计；若保留会造成重复计算。
- Production Data 月份列采用**动态展示**：按 Production Vol 实际存在的 `YYYY-MM` 月份全部展示，不再固定 6 个月。

### 10.6 Demand HS 逻辑
- `Demand HS = Demand LBE + Supply Protection`（按月）
- Supply 对应关系：
  - `Base` 加到 HS Base
  - `PP` / `Promotion` 加到 HS Promotion
- HS Total = Base + Promotion

### 10.7 IYA（月度）逻辑
- `Demand LBE IYA` 与 `Demand HS IYA`：
  - `IYA% = 当月值 / 去年同月值 * 100`
- 去年同月基线来自 `Historical Shipment Data_FY2425.xlsx`（`Sheet1`）
- 分母缺失或为 0 时显示 `-`

### 10.8 Demand IYA by quarter（季度）逻辑
- 季度标签按当前季度月份缩写（如 `JFM`）
- 行：`Base / Promotion / Total`
- 列：
  - `JFM LBE`：第一季度 LBE 合计
  - `JFM HS`：第一季度 HS 合计
  - `JFM LBE IYA`：第一季度 LBE / 去年同期 * 100
  - `JFM HS IYA`：第一季度 HS / 去年同期 * 100

### 10.9 Demand Assumption 页面布局
- 双列三行：
  - 第1行：`Demand LBE` | `Demand LBE IYA`
  - 第2行：`Demand HS` | `Demand HS IYA`
  - 第3行：`Supply Protection` | `Demand IYA by quarter`
- 标题不使用 `Table x:` 前缀

### 10.10 显示格式
- 月份统一：`YYYY-MM`
- Demand 数值表（LBE / HS）显示整数
- IYA 表显示百分比

## 11）页面功能总览（最新）
- `全局操作（页头）`
  - `Backup Snapshot`：一键导出当前看板快照（Excel + CSV 历史目录）。
  - `Refresh Mail & Open HTML`：一键刷新周报邮件内容并在新标签页打开最新 HTML 预览。
- `Demand Assumption`
  - 包含 `Demand LBE`、`Demand LBE IYA`、`Demand HS`、`Demand HS IYA`。
  - Supply Protection 拆分为 `(PP + Base)` 与 `(HKTW + ESS)` 两块。
  - 季度 IYA 拆分为当前季度与下季度两张表。
- `Supply Protection`
  - KPI 卡片：总保护量 MSU、Item Text 数量、未来 7 天 PDE 预警。
  - Role 趋势图、Role-Item 汇总矩阵、Monthly Summary、Past Due Alerts。
- `Project Details`
  - `Role × Item × Project` 汇总。
  - 支持可搜索多选筛选：`Requester Email` 与 `MRP Element Indicator`。
  - 支持明细下钻与明细导出。
- `Demand Data`
  - `TD Version Monthly Comparison` 主表。
  - 点击 GAP 行后可查看 `Level2 GAP Details` 与 `GAP Difference Details`。
  - 两类明细都支持导出。
- `Production Data`
  - `By Plant` 与 `By Plant / Level1 / Level2` 两张表。
  - 当月拆分为 `MTD`、`Left Production`、`Current Month`。
  - Total 行（含 `GC Total`）高亮显示。

## 12）板子逻辑 HTML 说明文档
- 已提供可交接的 HTML 文档：
  - `docs/board_logic_guide.html`
- 可直接浏览器打开，包含：
  - 页面功能说明，
  - 数据来源文件，
  - 主要计算逻辑与口径，
  - 刷新与运维建议。

## 13）GitHub 推送（网络不稳定时推荐）

### 13.1 常规推送
```powershell
git push origin main
```

### 13.2 若出现 TLS / schannel 断连，使用已验证成功方式
> 本项目在当前环境下已成功使用以下命令推送。

```powershell
$ok=$false; 1..3 | ForEach-Object { Write-Host "Push attempt $_"; git -c http.version=HTTP/1.1 -c http.schannelCheckRevoke=false push origin main; if ($LASTEXITCODE -eq 0) { $ok=$true; break } Start-Sleep -Seconds 2 }; if (-not $ok) { exit 1 }
```

说明：
- 使用 `HTTP/1.1` 降低部分网络环境下的 TLS 兼容问题。
- 关闭本次命令的 `schannel` 证书撤销检查（仅当前命令生效，不改全局配置）。
- 自动重试最多 3 次，任一次成功即停止。
