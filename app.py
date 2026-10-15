"""
WinResMon - Windows リソースモニター
  - CPU / メモリ / ネットワーク / ディスクI/O のリアルタイム表示
  - ディレクトリごとのストレージ容量スキャン（スナップショット保存・過去時点の表示・比較）

依存: psutil（必須）, pywebview（専用ウィンドウ表示。無ければブラウザで開く）
起動: python app.py            → 専用ウィンドウで開く
      python app.py --browser  → 既定のブラウザで開く
      python app.py --selftest → 動作確認だけして終了（終了コード 0 = 正常）
"""
import json
import os
import platform
import socket
import sys
import tempfile
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

APP_NAME = "WinResMon"
APP_VERSION = "1.0.0"
FROZEN = getattr(sys, "frozen", False)  # PyInstaller でビルドされた exe として動いているか

# 同梱ファイル（index.html / static）の場所：exe の場合は展開先、スクリプトの場合はこのフォルダ
RES_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
INDEX_HTML = RES_DIR / "index.html"
STATIC_DIR = RES_DIR / "static"


def _data_dir():
    """スナップショット等の保存先。インストール先（Program Files 等）は書き込めないため分ける"""
    if os.environ.get("WINRESMON_DATA"):
        return Path(os.environ["WINRESMON_DATA"])
    if sys.platform == "win32" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / APP_NAME
    if not FROZEN:
        return Path(__file__).resolve().parent
    return Path.home() / f".{APP_NAME.lower()}"


DATA_DIR = _data_dir()
SNAP_DIR = DATA_DIR / "snapshots"
SNAP_DIR.mkdir(parents=True, exist_ok=True)

# ウィンドウアプリ（--windowed）では標準出力が無いので、ログファイルへ振り向ける
if sys.stdout is None or sys.stderr is None:
    _log = open(DATA_DIR / "winresmon.log", "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stdout or _log
    sys.stderr = sys.stderr or _log

try:
    import psutil
except ImportError:
    print("psutil が必要です: pip install psutil")
    sys.exit(1)

HOST = "127.0.0.1"
PORT = int(os.environ.get("WINRESMON_PORT", "8765"))

MAX_CHILDREN = 60  # 1ノードあたり保持する子要素の上限（残りは「その他」にまとめる）


# ---------------------------------------------------------------------------
# リアルタイム統計（1秒ごとにバックグラウンドで採取）
# ---------------------------------------------------------------------------
class StatsCollector:
    def __init__(self, interval=1.0):
        self.interval = interval
        self.lock = threading.Lock()
        self.latest = {}
        self._prev_net = psutil.net_io_counters(pernic=True)
        self._prev_disk = psutil.disk_io_counters()
        self._prev_t = time.time()
        psutil.cpu_percent(percpu=True)  # 初回呼び出しは0を返すため空打ち
        t = threading.Thread(target=self._loop, daemon=True)
        t.start()

    def _loop(self):
        while True:
            time.sleep(self.interval)
            try:
                self._sample()
            except Exception as e:  # 採取失敗でスレッドを止めない
                print("stats error:", e)

    def _sample(self):
        now = time.time()
        dt = max(now - self._prev_t, 1e-6)

        per_cpu = psutil.cpu_percent(percpu=True)
        freq = psutil.cpu_freq()
        vm = psutil.virtual_memory()
        sm = psutil.swap_memory()

        net = psutil.net_io_counters(pernic=True)
        nics = []
        tot_up = tot_down = 0.0
        stats = psutil.net_if_stats()
        for name, c in net.items():
            p = self._prev_net.get(name)
            if not p:
                continue
            up = max(c.bytes_sent - p.bytes_sent, 0) / dt
            down = max(c.bytes_recv - p.bytes_recv, 0) / dt
            st = stats.get(name)
            if st and not st.isup:
                continue
            if "loopback" in name.lower():
                continue
            nics.append({"name": name, "up": up, "down": down})
            tot_up += up
            tot_down += down
        self._prev_net = net

        disk_io = psutil.disk_io_counters()
        read_bps = write_bps = 0.0
        if disk_io and self._prev_disk:
            read_bps = max(disk_io.read_bytes - self._prev_disk.read_bytes, 0) / dt
            write_bps = max(disk_io.write_bytes - self._prev_disk.write_bytes, 0) / dt
        self._prev_disk = disk_io
        self._prev_t = now

        # 上位プロセス（CPU / メモリ）
        procs = []
        for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_info"]):
            try:
                info = p.info
                procs.append({
                    "pid": info["pid"],
                    "name": info["name"] or "?",
                    "cpu": (info["cpu_percent"] or 0.0) / max(psutil.cpu_count() or 1, 1),
                    "mem": info["memory_info"].rss if info["memory_info"] else 0,
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        top_cpu = sorted(procs, key=lambda x: x["cpu"], reverse=True)[:8]
        top_mem = sorted(procs, key=lambda x: x["mem"], reverse=True)[:8]

        data = {
            "time": now,
            "cpu": {
                "total": sum(per_cpu) / len(per_cpu) if per_cpu else 0,
                "per_core": per_cpu,
                "freq_mhz": freq.current if freq else None,
            },
            "memory": {
                "total": vm.total, "used": vm.used, "available": vm.available,
                "percent": vm.percent,
                "swap_total": sm.total, "swap_used": sm.used, "swap_percent": sm.percent,
            },
            "network": {"up": tot_up, "down": tot_down, "nics": nics},
            "disk_io": {"read": read_bps, "write": write_bps},
            "top_cpu": top_cpu,
            "top_mem": top_mem,
            "proc_count": len(procs),
        }
        with self.lock:
            self.latest = data

    def get(self):
        with self.lock:
            return dict(self.latest)


def is_admin():
    """Windows: 管理者権限で動いているか。Windows 以外は root かどうか"""
    if sys.platform == "win32":
        try:
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    return hasattr(os, "geteuid") and os.geteuid() == 0


def relaunch_elevated():
    """UAC の確認を出して、管理者権限で自分自身を起動し直す。成功したら True"""
    if sys.platform != "win32":
        return False
    import ctypes
    import subprocess
    args = [a for a in sys.argv[1:] if a != "--elevate"]
    if FROZEN:
        exe, params = sys.executable, subprocess.list2cmdline(args)
    else:
        exe, params = sys.executable, subprocess.list2cmdline([os.path.abspath(__file__)] + args)
    # 戻り値が 32 より大きければ起動成功（UAC でキャンセルされると 5 などが返る）
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)
    return rc > 32


def system_info():
    boot = datetime.fromtimestamp(psutil.boot_time())
    return {
        "hostname": platform.node(),
        "os": f"{platform.system()} {platform.release()} ({platform.version()})",
        "cores_logical": psutil.cpu_count(),
        "cores_physical": psutil.cpu_count(logical=False),
        "boot_time": boot.strftime("%Y-%m-%d %H:%M:%S"),
        "platform": sys.platform,
        "app_version": APP_VERSION,
        "is_admin": is_admin(),
        "can_elevate": sys.platform == "win32" and not is_admin(),
        "data_dir": str(DATA_DIR),
    }


def list_drives():
    drives = []
    for p in psutil.disk_partitions(all=False):
        if "cdrom" in p.opts or not p.fstype:
            continue
        try:
            u = psutil.disk_usage(p.mountpoint)
        except (PermissionError, OSError):
            continue
        drives.append({
            "mount": p.mountpoint, "fstype": p.fstype,
            "total": u.total, "used": u.used, "free": u.free, "percent": u.percent,
        })
    return drives


# ---------------------------------------------------------------------------
# ディレクトリ容量スキャン
# ---------------------------------------------------------------------------
def _is_link(entry):
    try:
        if entry.is_symlink():
            return True
        # Windows のジャンクション（例: C:\Documents and Settings）を辿らない
        if hasattr(os.path, "isjunction") and os.path.isjunction(entry.path):
            return True
    except OSError:
        return True
    return False


class Scanner:
    def __init__(self):
        self.lock = threading.Lock()
        self.state = {"running": False}
        self._cancel = False

    def status(self):
        with self.lock:
            return dict(self.state)

    def cancel(self):
        self._cancel = True

    def start(self, root, depth, note=""):
        with self.lock:
            if self.state.get("running"):
                return False, "スキャン実行中です"
            if not os.path.isdir(root):
                return False, f"フォルダが見つかりません: {root}"
            self._cancel = False
            self.state = {
                "running": True, "root": root, "files": 0, "dirs": 0,
                "bytes": 0, "errors": 0, "current": root, "started": time.time(),
            }
        threading.Thread(target=self._run, args=(root, depth, note), daemon=True).start()
        return True, "started"

    def _walk(self, path, level, max_depth):
        """path 以下の合計サイズを返し、max_depth までのツリーを構築する"""
        node = {"name": os.path.basename(path.rstrip("\\/")) or path, "path": path,
                "size": 0, "files": 0, "children": []}
        file_bytes = 0
        file_count = 0
        try:
            with os.scandir(path) as it:
                entries = list(it)
        except (PermissionError, FileNotFoundError, OSError):
            with self.lock:
                self.state["errors"] += 1
            node["denied"] = True
            return node

        with self.lock:
            self.state["current"] = path
            self.state["dirs"] += 1

        for e in entries:
            if self._cancel:
                break
            try:
                if _is_link(e):
                    continue
                if e.is_dir(follow_symlinks=False):
                    child = self._walk(e.path, level + 1, max_depth)
                    node["size"] += child["size"]
                    node["files"] += child["files"]
                    if level < max_depth:
                        node["children"].append(child)
                else:
                    sz = e.stat(follow_symlinks=False).st_size
                    file_bytes += sz
                    file_count += 1
            except (PermissionError, FileNotFoundError, OSError):
                with self.lock:
                    self.state["errors"] += 1

        node["size"] += file_bytes
        node["files"] += file_count
        with self.lock:
            self.state["files"] += file_count
            self.state["bytes"] += file_bytes

        if level < max_depth:
            if file_bytes > 0:
                node["children"].append({"name": "(このフォルダ直下のファイル)", "path": path,
                                         "size": file_bytes, "files": file_count, "is_files": True})
            node["children"].sort(key=lambda c: c["size"], reverse=True)
            if len(node["children"]) > MAX_CHILDREN:
                rest = node["children"][MAX_CHILDREN:]
                node["children"] = node["children"][:MAX_CHILDREN]
                node["children"].append({"name": f"(その他 {len(rest)} 件)", "path": path,
                                         "size": sum(c["size"] for c in rest),
                                         "files": sum(c["files"] for c in rest), "is_other": True})
        if not node["children"]:
            node.pop("children")
        return node

    def _run(self, root, depth, note):
        t0 = time.time()
        try:
            tree = self._walk(root, 0, depth)
            cancelled = self._cancel
            ts = datetime.now()
            snap_id = ts.strftime("%Y%m%d_%H%M%S")
            disk = None
            try:
                u = psutil.disk_usage(root)
                disk = {"total": u.total, "used": u.used, "free": u.free}
            except OSError:
                pass
            snap = {
                "id": snap_id, "root": root, "depth": depth, "note": note,
                "created": ts.strftime("%Y-%m-%d %H:%M:%S"),
                "duration_sec": round(time.time() - t0, 1),
                "cancelled": cancelled, "disk": disk,
                "errors": self.state["errors"], "tree": tree,
            }
            (SNAP_DIR / f"{snap_id}.json").write_text(
                json.dumps(snap, ensure_ascii=False), encoding="utf-8")
            with self.lock:
                self.state.update({"running": False, "done": True, "snapshot": snap_id,
                                   "cancelled": cancelled})
        except Exception as ex:
            with self.lock:
                self.state.update({"running": False, "error": str(ex)})


def list_snapshots():
    out = []
    for f in sorted(SNAP_DIR.glob("*.json"), reverse=True):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            out.append({"id": d["id"], "root": d["root"], "created": d["created"],
                        "size": d["tree"]["size"], "files": d["tree"]["files"],
                        "note": d.get("note", ""), "cancelled": d.get("cancelled", False),
                        "depth": d.get("depth")})
        except Exception:
            continue
    return out


def load_snapshot(snap_id):
    safe = "".join(ch for ch in snap_id if ch.isalnum() or ch == "_")
    f = SNAP_DIR / f"{safe}.json"
    if not f.exists():
        return None
    return json.loads(f.read_text(encoding="utf-8"))


def delete_snapshot(snap_id):
    safe = "".join(ch for ch in snap_id if ch.isalnum() or ch == "_")
    f = SNAP_DIR / f"{safe}.json"
    if f.exists():
        f.unlink()
        return True
    return False


# ---------------------------------------------------------------------------
# HTTP サーバー
# ---------------------------------------------------------------------------
collector = None
scanner = Scanner()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # アクセスログは出さない

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        p = u.path
        if p in ("/", "/index.html"):
            body = INDEX_HTML.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif p.startswith("/static/"):
            name = os.path.basename(p)  # サブパスは許可しない
            f = STATIC_DIR / name
            if not f.is_file():
                return self._json({"error": "not found"}, 404)
            body = f.read_bytes()
            ctype = {".js": "application/javascript", ".css": "text/css"}.get(f.suffix, "application/octet-stream")
            self.send_response(200)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "max-age=86400")
            self.end_headers()
            self.wfile.write(body)
        elif p == "/api/stats":
            self._json(collector.get())
        elif p == "/api/system":
            self._json(system_info())
        elif p == "/api/drives":
            self._json(list_drives())
        elif p == "/api/scan/status":
            self._json(scanner.status())
        elif p == "/api/snapshots":
            self._json(list_snapshots())
        elif p == "/api/snapshot":
            d = load_snapshot(q.get("id", [""])[0])
            self._json(d if d else {"error": "not found"}, 200 if d else 404)
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0) or 0)
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            data = {}
        if u.path == "/api/scan":
            root = data.get("path", "").strip()
            depth = max(1, min(int(data.get("depth", 4)), 8))
            ok, msg = scanner.start(root, depth, data.get("note", ""))
            self._json({"ok": ok, "message": msg}, 200 if ok else 400)
        elif u.path == "/api/scan/cancel":
            scanner.cancel()
            self._json({"ok": True})
        elif u.path == "/api/elevate":
            ok = relaunch_elevated()
            self._json({"ok": ok, "message": "" if ok else "管理者として起動できませんでした（キャンセルされたか、権限がありません）"})
            if ok:
                # 新しいウィンドウが立ち上がるので、こちらは少し待って終了
                threading.Timer(0.8, lambda: os._exit(0)).start()
        elif u.path == "/api/snapshot/delete":
            self._json({"ok": delete_snapshot(data.get("id", ""))})
        else:
            self._json({"error": "not found"}, 404)


def _make_server():
    """既定ポートが使用中（多重起動など）なら空いているポートを自動で使う"""
    try:
        return ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError:
        return ThreadingHTTPServer((HOST, 0), Handler)


def selftest():
    """インストーラー作成時の自動確認用。統計採取・スキャン・HTTP応答を一通り試す"""
    import urllib.request
    global collector
    result = {"ok": False, "steps": []}
    try:
        collector = StatsCollector(0.5)
        server = _make_server()
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://{HOST}:{server.server_address[1]}"
        time.sleep(2)

        def get(path):
            with urllib.request.urlopen(base + path, timeout=10) as r:
                return r.status, r.read()

        st, body = get("/api/stats")
        stats = json.loads(body)
        assert st == 200 and "cpu" in stats and "memory" in stats, "stats"
        result["steps"].append(f"stats ok cpu={stats['cpu']['total']:.1f}% mem={stats['memory']['percent']}%")

        st, body = get("/")
        assert st == 200 and b"WinResMon" in body, "index"
        st, body = get("/static/echarts.min.js")
        assert st == 200 and len(body) > 100000, "echarts"
        result["steps"].append("index/static ok")

        drives = json.loads(get("/api/drives")[1])
        assert drives, "drives"
        result["steps"].append(f"drives ok {[d['mount'] for d in drives]}")

        tmp = Path(tempfile.mkdtemp(prefix="wrm_selftest_"))
        (tmp / "a").mkdir()
        (tmp / "a" / "f.bin").write_bytes(b"x" * 5000)
        (tmp / "g.txt").write_bytes(b"y" * 1000)
        ok, msg = scanner.start(str(tmp), 3, "selftest")
        assert ok, msg
        for _ in range(100):
            if not scanner.status().get("running"):
                break
            time.sleep(0.1)
        s = scanner.status()
        snap = load_snapshot(s["snapshot"])
        assert snap["tree"]["size"] == 6000, f"scan size {snap['tree']['size']}"
        delete_snapshot(s["snapshot"])
        result["steps"].append("scan ok")
        result["ok"] = True
    except Exception as e:
        import traceback
        result["error"] = f"{e}\n{traceback.format_exc()}"
    out = os.environ.get("WINRESMON_SELFTEST_OUT")
    if out:
        Path(out).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


def main():
    global collector
    if "--selftest" in sys.argv:
        sys.exit(selftest())

    # スタートメニュー「管理者として起動」から呼ばれたとき
    if "--elevate" in sys.argv and not is_admin():
        if relaunch_elevated():
            return
        print("管理者として起動できなかったため、通常権限で起動します")

    collector = StatsCollector(1.0)
    server = _make_server()
    url = f"http://{HOST}:{server.server_address[1]}/"
    print(f"{APP_NAME} {APP_VERSION} 起動: {url}  データ: {DATA_DIR}")

    use_window = "--browser" not in sys.argv and "--no-browser" not in sys.argv
    webview = None
    if use_window:
        try:
            import webview  # pywebview
        except Exception as e:
            print("pywebview が使えないためブラウザで開きます:", e)

    if webview:
        # サーバーは裏で動かし、ウィンドウを閉じたらアプリ終了
        threading.Thread(target=server.serve_forever, daemon=True).start()
        webview.create_window(f"{APP_NAME} - リソースモニター", url,
                              width=1400, height=900, min_size=(900, 600))
        webview.start()
        return

    if "--no-browser" not in sys.argv:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("終了します")


if __name__ == "__main__":
    main()
