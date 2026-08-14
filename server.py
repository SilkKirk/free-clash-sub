"""提供免费的 Clash 订阅地址，并定时自动抓取更新。"""

import datetime
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

_lock = threading.Lock()
_updated_at = None


def refresh(force=False):
    if not force and os.path.exists(OUT_FILE):
        age = time.time() - os.path.getmtime(OUT_FILE)
        if age < REFRESH_HOURS * 3600:
            return False
    with _lock:
        state = crawler.run_crawl()
        global _updated_at
        _updated_at = state["updated_at"]
        return True


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def background_worker():
    while True:
        try:
            refresh()
        except Exception:
            log.exception("后台抓取失败，将在 1 小时后重试")
        time.sleep(3600)


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
    refresh()
    if not os.path.exists(OUT_FILE):
        return Response("订阅尚未生成，请稍后重试", status=503, content_type="text/plain")
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


@app.get("/status")
def status():
    state = load_state()
    state["service_updated_at"] = _updated_at
    state["ok"] = bool(state.get("node_count"))
    return jsonify(state)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))

    def _bootstrap():
        try:
            if not os.path.exists(OUT_FILE):
                log.info("首次运行，立即抓取...")
                refresh(force=True)
            else:
                refresh()
            log.info("订阅文件就绪: %s", OUT_FILE)
        except Exception:
            log.exception("初始抓取失败，将按定时任务重试")

    threading.Thread(target=_bootstrap, daemon=True).start()
    threading.Thread(target=background_worker, daemon=True).start()

    app.run(host="0.0.0.0", port=port)