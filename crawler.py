"""从多个站点抓取每日最新免费 Clash 节点，聚合去重、真实测速后只保留最快的 N 个。

流程：
  1. 对每个站点：拉取列表页 → 优先找"今天"的文章，没有则用最新一篇
  2. 解析文章正文中的 YAML 订阅链接并下载
  3. 合并所有站点节点并去重
  4. 用 mihomo 真实测速（延迟），只保留最快的 N 个
  5. 生成最终订阅配置
"""

import datetime
import json
import logging
import os
import re
import sys
import time

import requests
import yaml

import speedtest

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
OUT_FILE = os.path.join(DATA_DIR, "subscription.yaml")
STATE_FILE = os.path.join(DATA_DIR, "state.json")

# 数据源配置：列表页 + YAML 订阅链接域名
SOURCES = [
    {
        "name": "lancex",
        "site": "https://lancex.net",
        "list_url": "https://lancex.net/free-node/",
        "yaml_domains": ["node.lancex.net"],
    },
    {
        "name": "clash-node",
        "site": "https://clash-node.com",
        "list_url": "https://clash-node.com/free-node/",
        "yaml_domains": ["node.clash-node.com"],
    },
]

TOP_N = int(os.environ.get("TOP_N", "30"))
DELAY_TIMEOUT_MS = int(os.environ.get("DELAY_TIMEOUT_MS", "8000"))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
}

ARTICLE_URL_RE = re.compile(r'<a href="(/free-node/[^"]+)"')
ARTICLE_DATE_RE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")

log = logging.getLogger("crawler")


def _session():
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def _today():
    return datetime.date.today()


def _article_date(href):
    m = ARTICLE_DATE_RE.search(href)
    if not m:
        return None
    try:
        return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def pick_article_url(session, source):
    """列表页取文章。优先今天发布的，没有则用最新一篇。"""
    resp = session.get(source["list_url"], timeout=30)
    resp.raise_for_status()
    hrefs = list(dict.fromkeys(ARTICLE_URL_RE.findall(resp.text)))
    if not hrefs:
        raise RuntimeError(f"{source['name']} 列表页未找到文章")

    today = _today()
    for href in hrefs:
        if _article_date(href) == today:
            log.info("[%s] 找到今日文章: %s", source["name"], href)
            return source["site"] + href
    log.warning("[%s] 今天(%.4d-%.2d-%.2d)的文章还没发布，使用最新一篇: %s",
                source["name"], today.year, today.month, today.day, hrefs[0])
    return source["site"] + hrefs[0]


def _yaml_re(domains):
    dom = "|".join(re.escape(d) for d in domains)
    return re.compile(rf"https://(?:{dom})/uploads/[^\s\"`'<>&]+\.yaml")


def fetch_article_yaml_urls(session, source, article_url):
    resp = session.get(article_url, timeout=30)
    resp.raise_for_status()
    urls = list(dict.fromkeys(_yaml_re(source["yaml_domains"]).findall(resp.text)))
    if not urls:
        raise RuntimeError(f"文章 {article_url} 中未找到 YAML 订阅链接")
    return urls


def download_yaml(session, url):
    resp = session.get(url, timeout=60)
    resp.raise_for_status()
    return resp.content.decode("utf-8", errors="replace")


def parse_yaml(text):
    try:
        return yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        log.warning("解析 YAML 失败: %s", e)
        return {}


def _node_key(proxy):
    return (
        proxy.get("type", ""),
        proxy.get("server", ""),
        proxy.get("port", ""),
        proxy.get("uuid", "") or proxy.get("password", ""),
    )


PLACEHOLDER_CREDENTIALS = {"pass", "password", "user", "username", "123", "123456", "admin"}


def _is_junk(proxy):
    """过滤掉明显是占位凭证的公共 http/socks5 垃圾节点。"""
    if proxy.get("type") not in ("http", "socks5"):
        return False
    user = str(proxy.get("username") or "").lower()
    pwd = str(proxy.get("password") or "").lower()
    if user in PLACEHOLDER_CREDENTIALS or pwd in PLACEHOLDER_CREDENTIALS:
        return True
    if not user and not pwd:
        return False
    return False


def _dedupe_names(proxies):
    """mihomo 要求代理名唯一：对同名的不同节点追加序号后缀。"""
    counts = {}
    for p in proxies:
        counts[p["name"]] = counts.get(p["name"], 0) + 1
    used = {}
    for p in proxies:
        n = p["name"]
        if counts[n] == 1:
            continue
        idx = used.get(n, 0) + 1
        used[n] = idx
        p["name"] = f"{n} #{idx}"
    return proxies


def merge_proxies(docs):
    merged, seen = [], set()
    for doc in docs:
        for p in doc.get("proxies", []):
            if not isinstance(p, dict) or not p.get("name") or not p.get("server"):
                continue
            if _is_junk(p):
                continue
            key = _node_key(p)
            if key in seen:
                continue
            seen.add(key)
            merged.append(p)
    return _dedupe_names(merged)


def build_proxy_groups(proxies):
    names = [p["name"] for p in proxies]
    return [
        {
            "name": "节点选择",
            "type": "select",
            "proxies": ["自动选择", "DIRECT"] + names,
        },
        {
            "name": "自动选择",
            "type": "url-test",
            "url": "http://www.gstatic.com/generate_204",
            "interval": 300,
            "tolerance": 50,
            "proxies": names,
        },
    ]


# mihomo 内置 DIRECT/REJECT，无需自定义组；
# 不用 GEOIP 规则避免客户端依赖 geodata 数据库下载
RULES = [
    "MATCH,节点选择",
]


def build_subscription(proxies, delays, sources_info):
    cfg = {
        "mixed-port": 7890,
        "allow-lan": False,
        "mode": "rule",
        "log-level": "info",
        "ipv6": False,
        "external-controller": "127.0.0.1:9090",
        "dns": {
            "enable": True,
            "listen": "127.0.0.1:1053",
            "enhanced-mode": "fake-ip",
            "fake-ip-range": "198.18.0.1/16",
            "default-nameserver": ["223.5.5.5", "119.29.29.29"],
            "nameserver": [
                "https://dns.alidns.com/dns-query",
                "https://doh.pub/dns-query",
            ],
            "proxy-server-nameserver": [
                "https://dns.alidns.com/dns-query",
                "https://doh.pub/dns-query",
            ],
        },
        "proxies": proxies,
        "proxy-groups": build_proxy_groups(proxies),
        "rules": RULES,
    }
    lines = [
        "# Free Clash Subscription (speed-tested, top " + str(len(proxies)) + ")",
        f"# generated: {datetime.datetime.now():%Y-%m-%d %H:%M:%S}",
        "# sources:",
    ]
    for si in sources_info:
        lines.append(f"#   - [{si['source']}] {si['article_url']}")
    lines.append("# node delays:")
    for p in proxies:
        d = delays.get(p["name"])
        if d is not None:
            lines.append(f"#   {d:>5}ms  {p['name']}")
    body = yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False, width=400)
    return "\n".join(lines) + "\n\n" + body


def _validate_subscription():
    """用 mihomo -t 校验最终订阅配置，防止生成坏配置。"""
    import subprocess as sp

    bin_path = speedtest.ensure_mihomo()
    r = sp.run(
        [bin_path, "-t", "-d", speedtest.TEST_DIR, "-f", OUT_FILE],
        capture_output=True, text=True, timeout=60,
    )
    if r.returncode != 0:
        raise RuntimeError(f"订阅配置校验失败: {(r.stdout + r.stderr)[-800:]}")


def run_crawl(top_n=None, force_speed_test=True):
    top_n = top_n or TOP_N
    os.makedirs(DATA_DIR, exist_ok=True)
    session = _session()

    all_proxies, sources_info, source_urls = [], [], []
    for source in SOURCES:
        try:
            article_url = pick_article_url(session, source)
            urls = fetch_article_yaml_urls(session, source, article_url)
            log.info("[%s] 找到 %d 个 YAML 源: %s", source["name"], len(urls), urls)
            docs = [parse_yaml(download_yaml(session, u)) for u in urls]
            merged = merge_proxies(docs)
            log.info("[%s] 去重后 %d 个节点", source["name"], len(merged))
            all_proxies.extend(merged)
            sources_info.append({"source": source["name"], "article_url": article_url})
            source_urls.extend(urls)
        except Exception as e:
            log.exception("[%s] 抓取失败: %s", source["name"], e)

    if not all_proxies:
        raise RuntimeError("所有数据源抓取均失败")

    all_proxies = _dedupe_names(all_proxies)
    total = len(all_proxies)
    log.info("全源聚合去重后共 %d 个节点", total)

    if force_speed_test:
        top_proxies, delays = speedtest.speed_test(
            all_proxies, top_n=top_n, delay_timeout=DELAY_TIMEOUT_MS
        )
        if not top_proxies:
            raise RuntimeError("测速后没有可用节点")
    else:
        top_proxies, delays = all_proxies[:top_n], {}

    log.info("测速后保留最快 %d 个节点", len(top_proxies))
    sub = build_subscription(top_proxies, delays, sources_info)

    tmp = OUT_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(sub)
    os.replace(tmp, OUT_FILE)

    _validate_subscription()

    state = {
        "updated_at": datetime.datetime.now().isoformat(),
        "node_count": len(top_proxies),
        "total_candidates": total,
        "sources": sources_info,
        "all_yaml_urls": source_urls,
    }
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    log.info("订阅已生成: %s (%d 节点)", OUT_FILE, len(top_proxies))
    return state


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    for attempt in range(3):
        try:
            run_crawl()
            sys.exit(0)
        except Exception as e:
            log.exception("第 %d 次抓取失败: %s", attempt + 1, e)
            time.sleep(5)
    sys.exit(1)