# Free Clash Subscription

自动从多家免费节点站点抓取每日最新的 Clash 节点，聚合去重后**用 mihomo 核心真实测速**，只保留速度最快的 30 个节点，生成订阅地址，可用 GitHub Actions 每天自动更新。

## 功能

- 数据源：`lancex.net`、`clash-node.com`（每天发布新文章，URL 会变）
- 自动定位**今日最新文章**（先查今天有没有发布，没有则用最新一篇），提取全部 Clash YAML 订阅源
- 合并去重：按 `类型 + 服务器 + 端口 + 凭据` 去重，并处理跨站点重名节点
- 过滤掉明显是占位凭证的公共 http/socks5 垃圾节点
- **真实测速**：TCP 连通性过滤后，经 mihomo 核心实际穿透代理请求 `gstatic.com/generate_204` 测延迟，再通过下载 10MB 文件测带宽，只保留最快的 30 个
- 生成包含 `dns` / `proxy-groups(节点选择/自动选择)` / `rules` 的完整可用 Clash 配置，并用 `mihomo -t` 自动校验
- Web 服务（可选部署）：`/sub` 订阅地址、`/` 信息页、`/status` JSON 状态，后台定时刷新

## 推荐：GitHub Actions 每天自动更新（订阅地址永久不变）

项目已内置 `.github/workflows/daily-sub.yml`，每天 UTC 02:00（北京时间 10:00）自动执行一次完整流程并提交结果。

1. 在 GitHub 新建一个**公开**仓库（订阅地址要求公开才可被客户端直接拉取），例如 `free-clash-sub`
2. 把本项目推上去（见下方"推送到 GitHub"）
3. 仓库里 Actions → daily-sub → Run workflow 立即跑一次（或等次日定时）

之后订阅地址固定为：

```
https://raw.githubusercontent.com/<你的用户名>/<仓库名>/main/data/subscription.yaml
```

在 Clash / Mihomo / Clash Verge / ClashX 等客户端里"从 URL 导入配置"，填入该地址即可。客户端通常每天自动拉取新版本。

### 推送到 GitHub

方式一（推荐，如果有 GitHub CLI）：

```bash
gh repo create free-clash-sub --public --source . --remote origin --push
```

方式二（手动）：

```bash
git init
git add .
git commit -m "init"
git remote add origin https://github.com/<你的用户名>/free-clash-sub.git
git push -u origin main
```

> 私有仓库的 raw 地址需要鉴权，Clash 客户端无法直接订阅，请用公开仓库。

## 本地运行

```bash
pip install -r requirements.txt
python crawler.py      # 只生成 data/subscription.yaml（含测速，约 5-10 分钟）
python server.py       # 或起 Web 服务对外提供订阅
```

启动 Web 服务后订阅地址为 `http://<服务器IP>:5000/sub`。

## 用 Docker 部署

```bash
docker build -t free-clash-sub .
docker run -d --name free-clash-sub -p 5000:5000 \
  -e REFRESH_HOURS=6 \
  -e PORT=5000 \
  -v free-clash-data:/app/data \
  free-clash-sub
```

## 配置

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `PORT` | `5000` | Web 服务端口 |
| `REFRESH_HOURS` | `6` | 订阅自动刷新间隔（小时） |
| `PUBLIC_URL` | `你的服务器地址:5000` | 信息页展示的订阅地址前缀 |
| `TOP_N` | `30` | 测速后保留的最快节点数 |
| `DELAY_TIMEOUT_MS` | `2000` | 单节点延迟测速超时（毫秒） |
| `BANDWIDTH_TOP_N` | `60` | 进入带宽测试的节点数（延迟测速前 N 名） |
| `MIHOMO_MIRROR` | - | mihomo 下载首选镜像（默认直连 GitHub） |

## 项目结构

```
free-clash-sub/
├── crawler.py            # 多源抓取 / 合并去重 / 测速 / 生成订阅
├── speedtest.py          # mihomo 核心管理 + 延迟测速 + 带宽测速
├── server.py             # 订阅 Web 服务 + 定时刷新
├── requirements.txt
├── Dockerfile
├── .github/workflows/daily-sub.yml   # 每日自动更新
└── data/                 # 运行生成
    ├── subscription.yaml # 最终订阅（GitHub 上也提交这份）
    └── state.json        # 最近一次抓取状态
```

## 免责声明

免费节点来源为公开网络分享，节点稳定性和安全性无法保证，不建议用于重要数据传输。请遵守所在地区法律法规，合理使用。