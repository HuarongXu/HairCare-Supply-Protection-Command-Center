# Job Aid：在另外一台电脑从 GitHub 更新 MatRes 项目

## 适用场景
- 你已经有项目仓库权限。
- 你要在新电脑或第二台电脑上同步最新代码并启动看板。

---

## 一、首次在新电脑上拉取项目

### 1) 安装 Git（如未安装）
- 下载并安装 Git for Windows。
- 安装完成后，打开 PowerShell，执行：

```powershell
git --version
```

### 2) 克隆仓库
在你希望放项目的目录执行：

```powershell
git clone https://github.com/HuarongXu/HairCare-Supply-Protection-Command-Center.git
cd HairCare-Supply-Protection-Command-Center
```

### 3) 启动（推荐一键脚本）
双击运行：

- `One Click Start Up/start_matres.bat`

该脚本会自动：
- 检查 Python
- 创建当前电脑专属虚拟环境（`.venv_<你的电脑名>`）
- 安装依赖
- 启动 Dashboard

默认地址：
- `http://<本机IP>:8050`
- 本机也可用 `http://127.0.0.1:8050`

---

## 二、在另一台电脑更新到 GitHub 最新版本（推荐流程）

### 场景 A：目录已经是 Git 仓库（最常见）
进入项目目录后执行：

```powershell
git rev-parse --is-inside-work-tree
git checkout main
git pull origin main
```

然后同步依赖并重启服务：

```powershell
.\.venv\Scripts\activate
pip install -r requirements.txt
python .\scripts\matres_pipeline.py
python .\dashboards\matres_app.py
```

---

### 场景 B：你之前是下载 ZIP 解压（不是 Git 仓库）
如果 `git rev-parse --is-inside-work-tree` 报错，执行：

```powershell
git init
git remote add origin https://github.com/HuarongXu/HairCare-Supply-Protection-Command-Center.git
git fetch origin
git checkout -B main origin/main
```

> 若提示 `remote origin already exists`，可先执行：
> `git remote remove origin` 再重新 `git remote add origin ...`

---

### 场景 C：报错 “untracked files would be overwritten by checkout”
先备份本地配置，再清理未跟踪文件（保留虚拟环境和数据）：

```powershell
robocopy .\config .\_backup_config /E
git clean -fd -e .venv -e data -e _backup_config
git fetch origin
git checkout -B main origin/main
git reset --hard origin/main
```

---

### 启动脚本路径有空格时
请用引号运行（不要在 `.bat` 后面加 `\`）：

```powershell
& ".\One Click Start Up\start_matres.bat"
```

如果仓库里没有该脚本，则直接运行：

```powershell
python .\dashboards\matres_app.py
```

---

### 更新是否成功（检查）
```powershell
git log -1 --oneline
```

应显示最新提交（例如：`69ece6c ...` 或更新的 commit）。

---

## 三、如何把你本机改动再推送回 GitHub

> 推送前先确认你只提交需要的文件。

```powershell
git status
git add <文件1> <文件2>
git commit -m "你的提交说明"
git push origin main
```

示例：

```powershell
git add dashboards/matres_app.py docs/JOB_AID_GITHUB_UPDATE_CN.md
git commit -m "Update supply protection layout and add cross-PC GitHub update job aid"
git push origin main
```

---

## 四、常见问题（FAQ）

### Q1：为什么我访问 `http://<IP>:8050` 看到的还是旧页面？
常见原因：
- 旧 Python 进程还在占用 8050
- 浏览器缓存未清

处理方法：
1. 停掉旧进程（或关闭所有相关 Python 进程）
2. 重新启动 `start_matres.bat`
3. 浏览器强制刷新：`Ctrl + F5`

### Q2：为什么每次重启 PID 都变了？
- 这是正常现象。PID 是进程编号，重启就会变化。
- 关键是访问链接不变，服务监听端口（8050）正常即可。

### Q3：不同电脑的虚拟环境会冲突吗？
- 一键脚本使用 `.venv_<电脑名>`，就是为避免共享盘跨机器冲突。

---

## 五、推荐日常操作顺序（简版）
1. `git pull origin main`
2. 重启 `start_matres.bat`
3. `Ctrl + F5` 刷新页面
4. 验证功能
5. 需要回传改动时：`add -> commit -> push`
