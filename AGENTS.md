# AGENTS.md — 项目运维笔记

## Windows 开发机推送 GitHub 的网络问题（2026-09-20）

本机（Windows）直连 `git push` 到 `https://github.com/...` 不稳定，三种典型故障：

1. **本地代理故障**：环境变量里有 `HTTP_PROXY=http://127.0.0.1:49402`（WorkBuddy 沙箱代理），
   git 走该代理时 `CONNECT tunnel failed, response 502`（实测该代理对
   `api.github.com` 放行、对 `github.com` 主站 502）。
2. **直连瞬时超时**：`git -c http.proxy= -c https.proxy= ...` 绕过代理后，偶发
   `Failed to connect to github.com:443`。这是瞬时的，**重试 1-2 次即可成功**，
   fetch 实测能通。
3. 认证不依赖 credential helper（本机未配置），推送需要 PAT。

### 应急方案 A：git fetch 成功时，直接对齐远端

fetch 通了以后，**不要依赖 `origin/main` 引用**（见下方 ref 持久化问题），
直接用 FETCH_HEAD 里的 sha 硬对齐：

```bash
git fetch origin
cat .git/FETCH_HEAD        # 第一列就是远端 main 的 sha
git reset --hard <sha>
```

### 应急方案 B：api.github.com 推送（git push 彻底不通时）

`api.github.com` 在本机始终可直连。单文件用 Contents API：

```python
import base64, requests

TOKEN = os.environ["GITHUB_TOKEN"]  # PAT，需 repo 的 Contents: Read/Write 权限
REPO = "SilkKirk/free-clash-sub"
PATH, BRANCH = "speedtest.py", "main"
H = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json"}

# 1. 取现有文件的 sha（新文件可省略 sha）
r = requests.get(f"https://api.github.com/repos/{REPO}/contents/{PATH}",
                 headers=H, params={"ref": BRANCH}, timeout=15)
sha = r.json().get("sha")

# 2. PUT 新内容，生成 commit
r = requests.put(f"https://api.github.com/repos/{REPO}/contents/{PATH}", headers=H,
                 json={"message": "commit message", "content": base64.b64encode(
                     open(PATH, "rb").read()).decode(), "sha": sha, "branch": BRANCH},
                 timeout=15)
assert r.status_code == 200, r.text
```

多文件/带目录结构的提交用 Git Data API：POST blobs → 创建 tree（基于远端
base_commit 的 tree，只变更有改动的 path）→ 创建 commit（parent 指向远端 main
head）→ PATCH `/git/refs/heads/main` 更新 ref（返回 200 即成功）。

### 本地仓库与远端对账

API commit 不经过本地 git，推送后本地会与远端分叉。若本地待推提交与 API 提交
**内容一致**，对账方式：

```bash
git fetch origin            # 通了就直接来
git reset --hard <FETCH_HEAD 中的 sha>
```

内容不一致时，先把远端文件经 API 拉下来比对（注意远端可能是功能更全的新版本，
应以远端为准），再 reset。

## .git 整目录消失事故（2026-09-20）

ref 丢失问题恶化：一次 fetch 中途失败后 `.git` 整个目录消失（git 报 not a
repository），未推送的本地 commit 一并丢失，仅工作区文件幸存。

**恢复流程**（原地重建，不迁移动工作区）：

1. 把未推送的改动文件备份到仓库外（tools/_backup/）；
2. `git init -b main` + `git remote add origin <url>`，从 API 取远端 head 与
   作者身份（`git config user.name/email`）；
3. `git fetch origin`（重试）后 `git reset --hard <远端 sha>`；
4. 从备份恢复改动文件 → commit → `git push origin main`
   （新建仓库**必须显式带分支名**，否则报缺少 upstream）。

## .git/refs/remotes 松散引用瞬间丢失（2026-09-20 复现）

**症状**：`git fetch` 成功创建 `refs/remotes/origin/main`，但紧接着
`git rev-parse origin/main` 报 unknown revision——松散 ref 文件写入后立即消失，
连 `git update-ref refs/remotes/origin/main <sha>` 也一样（`refs/heads/` 下的
松散 ref 正常）。另外 packed-refs 手工追加时**必须用空格分隔 sha 与 ref 名**
（tab 会导致 git 不识别该行）。

**Workaround**：不依赖 refs/remotes 引用。fetch 后从 `.git/FETCH_HEAD` 取远端
sha，直接 `git reset --hard <sha>`；推送判断 ahead/behind 时用 API 查询远端 head。

## 其他注意事项

- GitHub Actions 每日订阅更新由 workflow 在云端跑，本地只需保证代码推上去即可。
- 2026-09-20：带宽测速 URL cachefly 长期不可达（mihomo 立即 502 → 带宽全 0），
  Cloudflare 端点对 runner IP 限流（429）且国内可达测不出穿墙速度，已改为
  **Google 系被墙端点优先**（dl.google.com，国内 SSL reset 只有节点能访问，
  测的是订阅真实可用带宽）+ 多端点降级链（429/403 全局切换下一个）；
  带宽测速经 mihomo listeners 每节点绑定独立本地端口并发执行（LISTEN_PORT_BASE
  起），不再切换 GLOBAL 组（并发会互相覆盖，串行又太慢）。

