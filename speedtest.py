"""基于 mihomo (Clash Meta) 核心的真实代理延迟测速，保留最快 N 个节点。"""

import json
import logging
import os
import platform
import socket
import subprocess
import sys
import time
import urllib.parse
import zipfile
from concurrent.futures import ThreadPoolExecutor

import requests
import yaml
from requests.adapters import HTTPAdapter

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TOOLS_DIR = os.path.join(BASE_DIR, "tools")
TEST_DIR = os.path.join(TOOLS_DIR, "mihomo-test")

CONTROLLER = "127.0.0.1:19097"
TEST_PORT = 17898
TEST_URL = "http://www.gstatic.com/generate_204"

MIHOMO_REPO = "MetaCubeX/mihomo"
MIHOMO_VERSION = "v1.19.29"
MIHOMO_ASSETS = {
    "Windows": "mihomo-windows-amd64-v1-go120-v1.19.29.zip",
    "Linux": "mihomo-linux-amd64-v1.19.29.gz",
    "Darwin": "mihomo-darwin-amd64-v1.19.29.gz",
}
# GitHub 下载：直连优先（GitHub Actions 运行器/大陆网络均可用），
# 失败后走加速镜像；可用环境变量 MIHOMO_MIRROR 指定首选镜像
MIHOMO_MIRRORS = [
    os.environ.get("MIHOMO_MIRROR"),
    None,  # 直连 GitHub
    "https://ghproxy.net",
    "https://gh-proxy.com",
    "https://ghproxy.cc",
]
GZ_URL = "https://github.com/{repo}/releases/download/{version}/{asset}"

log = logging.getLogger("speedtest")


def _asset_name():
    sysname = platform.system()
    return MIHOMO_ASSETS.get(sysname)


def _bin_name():
    return "mihomo" + (".exe" if platform.system() == "Windows" else "")


def _default_bin():
    """在 tools/mihomo 及相关目录中查找已存在的 mihomo 核心。"""
    import glob

    patterns = [
        os.path.join(TOOLS_DIR, "mihomo", "mihomo*"),
        os.path.join(TOOLS_DIR, "mihomo*"),
    ]
    for pat in patterns:
        for f in glob.glob(pat):
            if os.path.isfile(f) and not f.endswith((".zip", ".gz", ".yaml")):
                return f
    return None


def _mirror_urls(asset):
    url = GZ_URL.format(repo=MIHOMO_REPO, version=MIHOMO_VERSION, asset=asset)
    for mirror in MIHOMO_MIRRORS:
        if not mirror:
            yield url
        else:
            yield mirror + "/" + url.replace("https://", "")


def _verify_binary(path):
    sysname = platform.system()
    if sysname == "Windows":
        import zipfile

        with zipfile.ZipFile(path) as z:
            if z.testzip() is not None:
                return False
        return True
    with open(path, "rb") as f:
        return f.read(2) == b"\x1f\x8b"


def ensure_mihomo() -> str:
    """确保 mihomo 核心存在；缺失时经加速镜像下载（顺序尝试，校验完整性）。"""
    bin_path = _default_bin()
    if bin_path:
        return bin_path

    env_bin = os.environ.get("MIHOMO_BIN")
    if env_bin and os.path.exists(env_bin):
        return env_bin

    asset = _asset_name()
    if not asset:
        raise RuntimeError(f"不支持的平台: {platform.system()}")

    os.makedirs(TOOLS_DIR, exist_ok=True)
    sysname = platform.system()
    tmp = os.path.join(TOOLS_DIR, "mihomo-dl" + (".zip" if sysname == "Windows" else ".gz"))

    last_err = None
    for url in _mirror_urls(asset):
        try:
            log.info("下载 mihomo: %s", url)
            _download(url, tmp, timeout=60)
            if not _verify_binary(tmp):
                raise RuntimeError("下载文件完整性校验失败")
            break
        except Exception as e:
            last_err = e
            log.warning("下载失败(%s): %s，尝试下一个镜像", url, e)
    else:
        raise RuntimeError(f"所有镜像下载失败: {last_err}")

    if sysname == "Windows":
        dest = os.path.join(TOOLS_DIR, "mihomo")
        os.makedirs(dest, exist_ok=True)
        with zipfile.ZipFile(tmp) as z:
            for m in z.infolist():
                if m.filename.endswith((".exe", ".dll")):
                    out = os.path.join(dest, _bin_name() if m.filename.endswith(".exe") else os.path.basename(m.filename))
                    with z.open(m) as src, open(out, "wb") as dst:
                        dst.write(src.read())
        os.remove(tmp)
        bin_path = os.path.join(dest, _bin_name())
    else:
        import gzip

        out = os.path.join(TOOLS_DIR, _bin_name())
        with gzip.open(tmp, "rb") as src, open(out, "wb") as dst:
            dst.write(src.read())
        os.remove(tmp)
        os.chmod(out, 0o755)
        bin_path = out

    log.info("mihomo 核心已就绪: %s", bin_path)
    return bin_path


def _download(url, path, timeout):
    log.info("下载 %s", url)
    with requests.get(url, stream=True, timeout=timeout, headers={"User-Agent": "opencode"}) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        with open(path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 16):
                f.write(chunk)
                done += len(chunk)
                if done and done % (8 << 20) == 0 and total:
                    log.info("下载进度: %.1f/%.1f MB", done / 1e6, total / 1e6)
        log.info("下载完成: %d bytes", done)

def tcp_prefilter(proxies, workers=128, timeout=2.0):
    """先并发做 TCP 连通性过滤，剔除无法连上的节点。"""

    def _ok(p):
        try:
            with socket.create_connection((p["server"], int(p["port"])), timeout=timeout):
                return p
        except Exception:
            return None

    kept = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for r in ex.map(_ok, proxies):
            if r:
                kept.append(r)
    return kept


def _write_test_config(bin_path, proxies):
    os.makedirs(TEST_DIR, exist_ok=True)
    cfg = {
        "mixed-port": TEST_PORT,
        "allow-lan": False,
        "mode": "rule",
        "log-level": "warning",
        "external-controller": CONTROLLER,
        "proxies": proxies,
        "proxy-groups": [
            {"name": "GLOBAL", "type": "select", "proxies": [p["name"] for p in proxies]}
        ],
        "rules": ["MATCH,GLOBAL"],
    }
    cfg_path = os.path.join(TEST_DIR, "config.yaml")
    with open(cfg_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
    return cfg_path


def _wait_ready(proc, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"mihomo 进程提前退出 (code={proc.returncode})")
        try:
            r = requests.get(f"http://{CONTROLLER}/version", timeout=2)
            if r.status_code == 200:
                return
        except Exception:
            time.sleep(0.5)
    raise RuntimeError("mihomo 控制器未就绪")


def _delay_one(session, name, timeout_ms):
    quoted = urllib.parse.quote(name, safe="")
    url = f"http://{CONTROLLER}/proxies/{quoted}/delay?url={urllib.parse.quote(TEST_URL, safe='')}&timeout={timeout_ms}"
    r = session.get(url, timeout=timeout_ms / 1000 + 8)
    if r.status_code == 200:
        return r.json().get("delay")
    return None


BANDWIDTH_TEST_URL = "http://cachefly.cachefly.net/10mb.mp4"
GROUP_NAME = "GLOBAL"


def _switch_proxy(proxy_name):
    """通过 mihomo API 切换当前选中的代理节点。"""
    quoted = urllib.parse.quote(GROUP_NAME, safe="")
    url = f"http://{CONTROLLER}/proxies/{quoted}"
    r = requests.put(url, json={"name": proxy_name}, timeout=5)
    return r.status_code == 204


def _bandwidth_one(proxy_name, timeout=15):
    """通过代理下载测试文件，返回下载速度 (KB/s)，失败返回 0。"""
    if not _switch_proxy(proxy_name):
        return 0
    try:
        proxies = {
            "http": f"http://127.0.0.1:{TEST_PORT}",
            "https": f"http://127.0.0.1:{TEST_PORT}",
        }
        t0 = time.time()
        with requests.get(BANDWIDTH_TEST_URL, proxies=proxies, timeout=timeout, stream=True) as r:
            r.raise_for_status()
            total = 0
            for chunk in r.iter_content(chunk_size=1 << 16):
                total += len(chunk)
                if time.time() - t0 > timeout:
                    break
        elapsed = time.time() - t0
        if elapsed < 0.1 or total < 1024:
            return 0
        return total / elapsed / 1024  # KB/s
    except Exception:
        return 0


def speed_test(proxies, top_n=30, delay_timeout=2000, bandwidth_top_n=60, workers=64):
    """真实测速（实际请求穿透代理），返回 (最快节点列表, name->info 表)。

    流程：
      1. TCP 连通性过滤
      2. 延迟测速（timeout 低，快速过滤）
      3. 带宽测速（下载 10MB 文件，选真正快的）
      4. 按带宽排序取 top N
    """
    bin_path = ensure_mihomo()
    if not proxies:
        return [], {}

    log.info("阶段1: TCP 连通性过滤 (%d 节点)", len(proxies))
    candidates = tcp_prefilter(proxies)
    log.info("TCP 可达 %d 节点，进入真实测速", len(candidates))
    if not candidates:
        return [], {}

    cfg_path = _write_test_config(bin_path, candidates)
    log_file = os.path.join(TEST_DIR, "mihomo.log")
    args = [bin_path, "-d", TEST_DIR, "-f", cfg_path]
    with open(log_file, "wb") as lf:
        proc = subprocess.Popen(args, stdout=lf, stderr=lf)
    try:
        _wait_ready(proc)
        log.info("阶段2: 延迟测速 (%d 并发, 超时 %dms)", workers, delay_timeout)
        session = requests.Session()
        adapter = HTTPAdapter(pool_connections=workers, pool_maxsize=workers, max_retries=0)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        delay_results = {}

        def _test(p):
            name = p["name"]
            d = _delay_one(session, name, delay_timeout)
            return name, d, p

        done = 0
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for name, d, p in ex.map(_test, candidates):
                done += 1
                if d is not None:
                    delay_results[name] = d
                if done % 200 == 0:
                    log.info("延迟测速进度: %d/%d，可用 %d", done, len(candidates), len(delay_results))

        log.info("延迟可达节点: %d", len(delay_results))
        if not delay_results:
            return [], {}

        delay_ranked = sorted(
            (p for p in candidates if p["name"] in delay_results),
            key=lambda p: delay_results[p["name"]],
        )
        bandwidth_candidates = delay_ranked[:bandwidth_top_n]
        log.info("阶段3: 带宽测速 (%d 节点，下载 %s)", len(bandwidth_candidates), BANDWIDTH_TEST_URL)

        bandwidth_results = {}
        for i, p in enumerate(bandwidth_candidates):
            name = p["name"]
            speed = _bandwidth_one(name)
            if speed > 0:
                bandwidth_results[name] = speed
            if (i + 1) % 10 == 0:
                log.info("带宽测速进度: %d/%d，可用 %d", i + 1, len(bandwidth_candidates), len(bandwidth_results))

        log.info("带宽可用节点: %d", len(bandwidth_results))
        if not bandwidth_results:
            top = delay_ranked[:top_n]
            return top, {p["name"]: delay_results[p["name"]] for p in top}

        bw_ranked = sorted(
            (p for p in bandwidth_candidates if p["name"] in bandwidth_results),
            key=lambda p: bandwidth_results[p["name"]],
            reverse=True,
        )
        top = bw_ranked[:top_n]
        delays = {}
        for p in top:
            info = {}
            if p["name"] in delay_results:
                info["delay"] = delay_results[p["name"]]
            if p["name"] in bandwidth_results:
                info["bandwidth"] = round(bandwidth_results[p["name"]], 1)
            delays[p["name"]] = info
        return top, delays
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with open(os.path.join(BASE_DIR, "data", "subscription.yaml"), encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    t0 = time.time()
    top, delays = speed_test(doc["proxies"], top_n=30)
    print(f"耗时 {time.time() - t0:.0f}s, 保留 {len(top)} 节点")
    for p in top:
        info = delays.get(p["name"], {})
        delay_str = f"{info.get('delay', '?'):>5}ms" if 'delay' in info else "   N/A"
        bw_str = f"{info.get('bandwidth', 0):>7.1f}KB/s" if 'bandwidth' in info else "     N/A"
        print(f"  {delay_str}  {bw_str}  {p['server']}:{p['port']}  {p['name'][:40]}")