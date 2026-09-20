"""提供免费的 Clash 订阅地址，并定时自动抓取更新。

设计要点（针对 timeout 优化）：
  - /sub 永不阻塞：文件存在直接返回（过期则顺带触发后台刷新），
    文件不存在返回 503 + Retry-After，绝不在请求内同步抓取
  - 抓取单飞：同一时刻最多一个抓取在跑，重复触发自动跳过
  - 失败冷却：抓取结束（无论成败）后 MIN_RETRY_SECONDS 内不再触发，
    避免并发请求反复触发全量抓取导致雪崩
"""

import json
import logging
import os
import threading
import time

from flask import Flask, Response, jsonify, render_template_string

import crawler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("server")

app = Flask(__name__)

DATA_DIR = crawler.DATA_DIR
OUT_FILE = crawler.OUT_FILE
STATE_FILE = crawler.STATE_FILE

REFRESH_HOURS = float(os.environ.get("REFRESH_HOURS", "6"))
# 抓取结束（成功或失败）后的最小冷却时间（秒）
MIN_RETRY_SECONDS = float(os.environ.get("MIN_RETRY_SECONDS", "600"))
# 后台检查周期（秒）：只做文件年龄检查，真正的抓取由单飞 + 冷却控制
CHECK_INTERVAL_SECONDS = float(os.environ.get("CHECK_INTERVAL_SECONDS", "300"))

_lock = threading.Lock()
_refreshing = False
_last_refresh_done = 0.0
_updated_at = None


def _file_fresh():
    if not os.path.exists(OUT_FILE):
        return False
    return time.time() - os.path.getmtime(OUT_FILE) < REFRESH_HOURS * 3600


def _start_refresh(reason):
    """非阻塞触发一次抓取；已在跑或处于冷却期则跳过。返回是否真正启动。"""
    global _refreshing
    with _lock:
        if _refreshing:
            log.info("抓取已在进行中，跳过触发 (%s)", reason)
            return False
        if time.time() - _last_refresh_done < MIN_RETRY_SECONDS:
            log.info("距上次抓取结束不足 %.0fs，跳过触发 (%s)", MIN_RETRY_SECONDS, reason)
            return False
        _refreshing = True
    threading.Thread(target=_run_refresh, args=(reason,), daemon=True).start()
    return True


def _run_refresh(reason):
    global _refreshing, _last_refresh_done, _updated_at
    t0 = time.time()
    try:
        log.info("开始抓取更新 (%s)...", reason)
        state = crawler.run_crawl()
        _updated_at = state["updated_at"]
        log.info("抓取更新完成: %s 节点, 耗时 %.0fs",
                 state.get("node_count"), time.time() - t0)
    except Exception:
        log.exception("抓取更新失败 (%s), 耗时 %.0fs", reason, time.time() - t0)
    finally:
        with _lock:
            _refreshing = False
            _last_refresh_done = time.time()


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            log.exception("读取 state.json 失败")
    return {}


def background_worker():
    """定期检查订阅文件是否过期，过期则以非阻塞方式触发刷新。"""
    while True:
        time.sleep(CHECK_INTERVAL_SECONDS)
        try:
            if not _file_fresh():
                _start_refresh("scheduled")
        except Exception:
            log.exception("后台刷新检查失败")


@app.get("/")
def index():
    state = load_state()
    return render_template_string(
        """
        <!doctype html>
        <html lang="zh-CN"><head><meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Free Clash Subscription</title>
        <style>
            body{font-family:system-ui,sans-serif;max-width:720px;margin:40px auto;padding:0 16px;line-height:1.6}
            code{background:#f4f4f4;padding:2px 6px;border-radius:4px;word-break:break-all}
            .box{border:1px solid #ddd;border-radius:8px;padding:12px 16px;margin:16px 0}
        </style></head>
        <body>
        <h1>Free Clash Subscription</h1>
        <p>每日从 <a href="https://lancex.net/free-node/">lancex.net</a> 自动抓取最新免费节点并合并生成订阅，服务每 {{ refresh_hours }} 小时自动更新。</p>
        <div class="box">
            <b>订阅地址</b><br>
            <code>http://{{ host }}/sub</code>
            <p style="color:#666;font-size:14px">在 Clash / Mihomo 客户端中添加订阅并导入即可使用。</p>
        </div>
        <div class="box">
            <b>状态</b><br>
            节点数量: {{ state.get('node_count', '-') }}<br>
            最近更新: {{ state.get('updated_at', '-') }}<br>
            来源文章: <a href="{{ state.get('article_url', '#') }}">{{ state.get('article_url', '-') }}</a>
        </div>
        <p style="color:#999;font-size:13px">
            免费节点不稳定属正常现象，请勿用于非法用途。建议优先使用可靠付费服务。
        </p>
        </body></html>
        """,
        host=os.environ.get("PUBLIC_URL", "你的服务器地址:5000"),
        refresh_hours=REFRESH_HOURS,
        state=state,
    )


@app.get("/sub")
@app.get("/subscribe")
def subscribe():
    if os.path.exists(OUT_FILE):
        # 已过期则后台触发刷新，但当前请求仍然立即返回旧文件（绝不阻塞）
        if not _file_fresh():
            _start_refresh("subscription stale")
        with open(OUT_FILE, encoding="utf-8") as f:
            content = f.read()
        return Response(
            content,
            content_type="text/yaml; charset=utf-8",
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": 'inline; filename="subscription.yaml"',
            },
        )
    _start_refresh("subscription missing")
    return Response(
        "订阅尚未生成，后台抓取中，请稍后重试",
        status=503,
        content_type="text/plain; charset=utf-8",
        headers={"Retry-After": "30"},
    )


@app.get("/status")
def status():
    state = load_state()
    state["service_updated_at"] = _updated_at
    state["refreshing"] = _refreshing
    state["file_fresh"] = _file_fresh()
    state["ok"] = bool(state.get("node_count")) and os.path.exists(OUT_FILE)
    return jsonify(state)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))

    def _bootstrap():
        if not _file_fresh():
            log.info("订阅缺失或已过期，启动即触发抓取...")
            _start_refresh("bootstrap")
        else:
            log.info("订阅文件仍然新鲜，跳过启动抓取")

    threading.Thread(target=_bootstrap, daemon=True).start()
    threading.Thread(target=background_worker, daemon=True).start()

    try:
        from waitress import serve

        log.info("服务启动 (waitress): http://0.0.0.0:%s", port)
        serve(app, host="0.0.0.0", port=port, threads=8, channel_timeout=60)
    except ImportError:
        log.warning("waitress 未安装，回退到 Flask 开发服务器")
        app.run(host="0.0.0.0", port=port)
