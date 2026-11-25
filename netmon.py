"""
WinResMon「通信」タブ：どのアプリが、どこと、どれくらい通信しているか

  - 接続一覧：psutil.net_connections（PID 付き。管理者権限は不要）
  - 通信量・DNS 名：補助プログラム WinResMonNetAgent.exe（ETW、管理者権限が必要）
  - 記録：開始〜停止までに通信した宛先を集計して CSV / JSON に保存（対象プロセスを選べる）
  - 国別：宛先 IP → 国（DB-IP Country Lite）で集計し、地図とランキングに使う

旧 NetWatch（C# / WPF）の機能を WinResMon に統合したもの。
"""
import csv
import gzip
import ipaddress
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import psutil

try:
    import maxminddb
except ImportError:  # GeoIP なしでも他の機能は動かす
    maxminddb = None

CLOSED_LINGER = 5.0   # 閉じた接続を薄く表示しておく秒数
NEW_HIGHLIGHT = 3.0   # 新しい接続をハイライトする秒数
SHORT_LIVED = "(短命)"
_CGNAT = ipaddress.ip_network("100.64.0.0/10")


# ---------------------------------------------------------------------------
# IP アドレスの小道具
# ---------------------------------------------------------------------------
def norm_ip(ip):
    """IPv4-mapped IPv6 を IPv4 に揃え、スコープ ID を外す"""
    if not ip:
        return ""
    ip = ip.split("%", 1)[0]
    if ip.lower().startswith("::ffff:") and "." in ip:
        return ip[7:]
    return ip


def _addr(ip):
    try:
        return ipaddress.ip_address(ip)
    except ValueError:
        return None


def is_unspecified(ip):
    a = _addr(ip)
    return a is None or a.is_unspecified


def is_loopback(ip):
    a = _addr(ip)
    return bool(a and a.is_loopback)


def is_local_network(ip):
    """国に紐づかないアドレス（LAN・CGNAT・リンクローカル・マルチキャストなど）"""
    a = _addr(ip)
    if a is None:
        return True
    return (a.is_private or a.is_loopback or a.is_link_local or a.is_multicast
            or a.is_unspecified or a.is_reserved or (a.version == 4 and a in _CGNAT))


def endpoint(ip, port):
    return f"[{ip}]:{port}" if ":" in ip else f"{ip}:{port}"


# ---------------------------------------------------------------------------
# PID → プロセス名
# ---------------------------------------------------------------------------
class ProcNames:
    def __init__(self):
        self._cache = {}

    def get(self, pid):
        """(名前, 実行ファイルのパス)。PID は再利用されるので 30 秒で取り直す"""
        now = time.time()
        hit = self._cache.get(pid)
        if hit and now - hit[2] < 30:
            return hit[0], hit[1]
        if pid == 0:
            name, exe = "System Idle Process", None
        elif pid == 4:
            name, exe = "System", None
        else:
            try:
                p = psutil.Process(pid)
                name = p.name()
                try:
                    exe = p.exe() or None
                except (psutil.AccessDenied, psutil.ZombieProcess, OSError):
                    exe = None
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                name, exe = f"(終了済み {pid})", None
        self._cache[pid] = (name, exe, now)
        return name, exe


def running_process_names():
    names = set()
    for p in psutil.process_iter(["pid", "name"]):
        try:
            if p.info["pid"] == 0:
                continue
            names.add("System" if p.info["pid"] == 4 else (p.info["name"] or ""))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    names.discard("")
    return names


# ---------------------------------------------------------------------------
# 補助プログラム（ETW）
# ---------------------------------------------------------------------------
class EtwAgent:
    """WinResMonNetAgent.exe を起動し、JSON Lines の出力を読む"""

    def __init__(self, exe_path):
        self.exe = Path(exe_path) if exe_path else None
        self.lock = threading.Lock()
        self._pending = {}          # key -> [sent, recv]（前回 take() からの増分）
        self.dns = {}               # ip -> name
        self.state = {"available": bool(self.exe and self.exe.is_file()), "running": False,
                      "traffic": False, "dns": False, "error": None}
        self.ready = threading.Event()
        self.proc = None

    def start(self):
        if not self.state["available"]:
            self.state["error"] = "補助プログラムが見つかりません"
            return False
        flags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
        try:
            self.proc = subprocess.Popen([str(self.exe)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, creationflags=flags)
        except OSError as e:
            self.state["error"] = f"補助プログラムを起動できません: {e}"
            return False
        self.state["running"] = True
        threading.Thread(target=self._read, daemon=True).start()
        return True

    def _read(self):
        for line in self.proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            t = msg.get("type")
            if t == "traffic":
                with self.lock:
                    for proto, pid, la, lp, ra, rp, s, r in msg.get("rows", []):
                        k = (proto, pid, la, lp, ra, rp)
                        v = self._pending.setdefault(k, [0, 0])
                        v[0] += s
                        v[1] += r
            elif t == "dns":
                with self.lock:
                    for ip, name in msg.get("items", []):
                        self.dns[ip] = name
            elif t == "ready":
                self.state.update(traffic=msg.get("traffic", False), dns=msg.get("dns", False),
                                  error=msg.get("error"))
                self.ready.set()
            elif t == "error":
                self.state["error"] = msg.get("message")
                self.ready.set()
        self.state["running"] = False
        self.ready.set()

    def take(self):
        with self.lock:
            p, self._pending = self._pending, {}
        return p

    def host(self, ip):
        return self.dns.get(ip)

    def stop(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.close()
                self.proc.wait(3)
            except Exception:
                self.proc.kill()


# ---------------------------------------------------------------------------
# IP → 国
# ---------------------------------------------------------------------------
class GeoIp:
    FILE = "dbip-country-lite.mmdb"
    LOCAL = ("LAN", "ローカルネットワーク")
    UNKNOWN = ("??", "不明")

    def __init__(self, bundled_dir, user_dir):
        self.bundled = Path(bundled_dir) / self.FILE
        self.user = Path(user_dir) / self.FILE
        self.lock = threading.Lock()
        self.reader = None
        self.build_date = None
        self.error = None
        self._cache = {}
        self._open()

    def _open(self):
        with self.lock:
            self._cache = {}
            self.reader, self.error = None, None
            if maxminddb is None:
                self.error = "maxminddb がインストールされていません"
                return
            for p in sorted([p for p in (self.user, self.bundled) if p.is_file()],
                            key=lambda p: p.stat().st_mtime, reverse=True):
                try:
                    self.reader = maxminddb.open_database(str(p), maxminddb.MODE_MEMORY)
                    self.build_date = datetime.fromtimestamp(self.reader.metadata().build_epoch).strftime("%Y-%m-%d")
                    return
                except Exception as e:
                    self.error = str(e)
            self.error = self.error or "GeoIP データベースが見つかりません"

    def lookup(self, ip):
        """(ISO コード, 国名)"""
        hit = self._cache.get(ip)
        if hit:
            return hit
        if is_local_network(ip):
            res = self.LOCAL
        else:
            res = self.UNKNOWN
            with self.lock:
                if self.reader:
                    try:
                        rec = self.reader.get(ip) or {}
                        c = rec.get("country") or {}
                        iso = c.get("iso_code")
                        if iso:
                            names = c.get("names") or {}
                            res = (iso, names.get("ja") or names.get("en") or iso)
                    except Exception:
                        pass
        self._cache[ip] = res
        return res

    def update(self):
        """DB-IP から最新版をダウンロードする。月初は当月版がまだ無いことがあるので前月版も試す"""
        last = None
        now = datetime.utcnow()
        prev = (now.replace(day=1) - timedelta(days=1))
        for m in (now, prev):
            url = f"https://download.db-ip.com/free/dbip-country-lite-{m:%Y-%m}.mmdb.gz"
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "WinResMon"})
                with urllib.request.urlopen(req, timeout=120) as r:
                    data = gzip.decompress(r.read())
                tmp = self.user.with_suffix(".tmp")
                self.user.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_bytes(data)
                maxminddb.open_database(str(tmp), maxminddb.MODE_MEMORY).close()  # 壊れていないか確認
                os.replace(tmp, self.user)
                self._open()
                return f"{m:%Y-%m} 版に更新しました"
            except Exception as e:
                last = e
        raise RuntimeError(f"ダウンロードに失敗しました: {last}")


# ---------------------------------------------------------------------------
# 通信先の記録
# ---------------------------------------------------------------------------
class Recorder:
    """開始〜停止までに通信した宛先を（PID × 宛先）で集計する。ループバックは除く"""

    def __init__(self):
        self.lock = threading.Lock()
        self.recording = False
        self.started = self.stopped = None
        self.targets = None            # None = すべて / set(小文字のプロセス名)
        self.with_traffic = False
        self._recs = {}
        self._rejected = set()

    def start(self, targets, with_traffic):
        with self.lock:
            self._recs, self._rejected = {}, set()
            self.targets = None if targets is None else {t.lower() for t in targets}
            self.with_traffic = with_traffic
            self.started, self.stopped = datetime.now(), None
            self.recording = True

    def stop(self):
        with self.lock:
            self.recording = False
            self.stopped = datetime.now()

    def _rec(self, pid, proto, ra, rp, names):
        if pid == 0 or pid in self._rejected or is_unspecified(ra) or is_loopback(ra):
            return None
        key = (pid, proto, ra, rp)
        r = self._recs.get(key)
        if r is None:
            name, exe = names.get(pid)
            if self.targets is not None and name.lower() not in self.targets:
                self._rejected.add(pid)
                return None
            now = datetime.now()
            r = self._recs[key] = {"pid": pid, "proto": proto, "ip": ra, "port": rp, "name": name, "exe": exe,
                                   "host": None, "first": now, "last": now, "sent": 0, "recv": 0, "locals": set()}
        return r

    def observe(self, pid, proto, la, lp, ra, rp, names):
        with self.lock:
            if not self.recording:
                return
            r = self._rec(pid, proto, ra, rp, names)
            if r:
                r["last"] = datetime.now()
                r["locals"].add(f"{la}:{lp}")

    def add_bytes(self, pid, proto, la, lp, ra, rp, s, rv, names):
        with self.lock:
            if not self.recording:
                return
            r = self._rec(pid, proto, ra, rp, names)
            if r:
                r["last"] = datetime.now()
                r["sent"] += s
                r["recv"] += rv
                r["locals"].add(f"{la}:{lp}" if proto == "TCP" else f":{lp}")

    def count(self):
        with self.lock:
            return len(self._recs)

    def status(self):
        with self.lock:
            return {"recording": self.recording, "count": len(self._recs),
                    "started": self.started.strftime("%Y-%m-%d %H:%M:%S") if self.started else None,
                    "stopped": self.stopped.strftime("%Y-%m-%d %H:%M:%S") if self.stopped else None,
                    "elapsed": (((self.stopped or datetime.now()) - self.started).total_seconds()
                                if self.started else 0),
                    "targets": sorted(self.targets) if self.targets is not None else None,
                    "has_data": self.started is not None}

    def summary(self, host_of):
        """同じ実行ファイル × 宛先 を 1 行にまとめる"""
        with self.lock:
            groups = {}
            for r in self._recs.values():
                if not r["host"]:
                    r["host"] = host_of(r["ip"])
                k = ((r["exe"] or r["name"]).lower(), r["proto"], r["ip"], r["port"])
                g = groups.setdefault(k, {"name": r["name"], "exe": r["exe"], "pids": set(), "proto": r["proto"],
                                          "ip": r["ip"], "port": r["port"], "host": None, "first": r["first"],
                                          "last": r["last"], "conns": 0, "sent": 0, "recv": 0})
                g["pids"].add(r["pid"])
                g["host"] = g["host"] or r["host"]
                g["first"] = min(g["first"], r["first"])
                g["last"] = max(g["last"], r["last"])
                g["conns"] += len(r["locals"])
                g["sent"] += r["sent"]
                g["recv"] += r["recv"]
            rows = list(groups.values())
        rows.sort(key=lambda g: (g["name"].lower(), -(g["sent"] + g["recv"]), g["ip"]))
        return rows

    def save(self, folder, fmt, host_of):
        rows = self.summary(host_of)
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        stamp = (self.started or datetime.now()).strftime("%Y%m%d_%H%M%S")
        tf = "%Y-%m-%d %H:%M:%S"
        if fmt == "json":
            path = folder / f"NetRecord_{stamp}.json"
            doc = {"tool": "WinResMon", "startedAt": self.started.isoformat() if self.started else None,
                   "stoppedAt": self.stopped.isoformat() if self.stopped else None,
                   "trafficMeasured": self.with_traffic,
                   "targets": sorted(self.targets) if self.targets is not None else None,
                   "destinations": [{"process": g["name"], "path": g["exe"], "pids": sorted(g["pids"]),
                                     "protocol": g["proto"], "remoteIp": g["ip"], "remotePort": g["port"],
                                     "host": g["host"], "firstSeen": g["first"].isoformat(),
                                     "lastSeen": g["last"].isoformat(), "connections": g["conns"],
                                     "sentBytes": g["sent"], "receivedBytes": g["recv"]} for g in rows]}
            path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            path = folder / f"NetRecord_{stamp}.csv"
            with open(path, "w", newline="", encoding="utf-8-sig") as f:  # BOM 付きで Excel でも文字化けしない
                w = csv.writer(f)
                w.writerow(["プロセス名", "実行ファイル", "PID", "プロトコル", "リモートIP", "リモートポート", "ホスト名",
                            "初回", "最終", "接続数", "送信バイト", "受信バイト"])
                for g in rows:
                    w.writerow([g["name"], g["exe"] or "", " ".join(map(str, sorted(g["pids"]))), g["proto"],
                                g["ip"], g["port"], g["host"] or "", g["first"].strftime(tf), g["last"].strftime(tf),
                                g["conns"], g["sent"], g["recv"]])
        return str(path), len(rows)


# ---------------------------------------------------------------------------
# 宛先 IP ごとの累計（国別マップ用）
# ---------------------------------------------------------------------------
class RemoteStats:
    def __init__(self):
        self.lock = threading.Lock()
        self.since = datetime.now()
        self._by_ip = {}

    def reset(self):
        with self.lock:
            self._by_ip = {}
            self.since = datetime.now()

    def _get(self, pid, ra):
        if pid == 0 or is_unspecified(ra) or is_loopback(ra):
            return None
        return self._by_ip.setdefault(ra, {"sent": 0, "recv": 0, "pids": set(), "conns": set()})

    def observe(self, pid, la, lp, ra, rp):
        with self.lock:
            s = self._get(pid, ra)
            if s is not None:
                s["pids"].add(pid)
                s["conns"].add(f"T|{la}|{lp}|{rp}")

    def add_bytes(self, pid, proto, la, lp, ra, rp, sent, recv):
        with self.lock:
            s = self._get(pid, ra)
            if s is not None:
                s["pids"].add(pid)
                s["sent"] += sent
                s["recv"] += recv
                s["conns"].add(f"T|{la}|{lp}|{rp}" if proto == "TCP" else f"U|{lp}|{rp}")

    def snapshot(self):
        with self.lock:
            return [(ip, s["sent"], s["recv"], len(s["conns"]), set(s["pids"])) for ip, s in self._by_ip.items()]


# ---------------------------------------------------------------------------
# 本体
# ---------------------------------------------------------------------------
class NetMonitor:
    def __init__(self, data_dir, netdata_dir, agent_exe=None, admin=False, interval=1.0, start=True):
        self.data_dir = Path(data_dir)
        self.records_dir = self.data_dir / "net_records"
        self.settings_path = self.data_dir / "net_settings.json"
        self.interval = interval
        self.admin = admin
        self.lock = threading.Lock()
        self.names = ProcNames()
        self.pid_names = {}            # 終了したプロセスの名前を残す
        self.geo = GeoIp(netdata_dir, data_dir)
        self.recorder = Recorder()
        self.stats = RemoteStats()
        self.agent = EtwAgent(agent_exe)
        self.reverse_enabled = False
        self._reverse = {}
        self._pool = ThreadPoolExecutor(max_workers=4)
        self._rows = {}
        self._proc_bytes = {}          # pid -> [sent, recv] 累計
        self._latest = {"rows": [], "procs": [], "time": 0}
        self._prev_t = time.time()
        self._first = True
        self.settings = self._load_settings()
        if admin:
            self.agent.start()
        if start:
            threading.Thread(target=self._loop, daemon=True).start()

    # ----- 設定（記録対象） -----
    def _load_settings(self):
        try:
            d = json.loads(self.settings_path.read_text(encoding="utf-8"))
            return {"record_all": bool(d.get("record_all", True)),
                    "targets": [t for t in d.get("targets", []) if isinstance(t, str)]}
        except Exception:
            return {"record_all": True, "targets": []}

    def save_settings(self, record_all, targets):
        self.settings = {"record_all": bool(record_all), "targets": sorted({t for t in targets if isinstance(t, str) and t})}
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self.settings_path.write_text(json.dumps(self.settings, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass
        return self.settings

    def target_candidates(self):
        with self.lock:
            communicating = {r["proc"] for r in self._rows.values() if not r["closed"] and r["pid"] not in (0,)}
        running = running_process_names()
        selected = set(self.settings["targets"])
        sel_lower = {s.lower() for s in selected}
        names = running | communicating | selected
        items = [{"name": n, "selected": n.lower() in sel_lower, "running": n in running, "communicating": n in communicating}
                 for n in names]
        items.sort(key=lambda i: (not i["selected"], not i["communicating"], i["name"].lower()))
        return {"record_all": self.settings["record_all"], "items": items}

    # ----- 記録 -----
    def record_start(self, record_all=None, targets=None):
        if record_all is not None:
            self.save_settings(record_all, targets if targets is not None else self.settings["targets"])
        if not self.settings["record_all"] and not self.settings["targets"]:
            return False, "記録するプロセスが選ばれていません"
        self.recorder.start(None if self.settings["record_all"] else self.settings["targets"],
                            with_traffic=bool(self.agent.state.get("traffic")))
        return True, "started"

    def record_stop(self):
        self.recorder.stop()
        return self.recorder.status()

    def record_save(self, fmt):
        return self.recorder.save(self.records_dir, "json" if fmt == "json" else "csv", self.host_of)

    # ----- ホスト名 -----
    def host_of(self, ip):
        if not ip or is_unspecified(ip):
            return ""
        h = self.agent.host(ip)
        if h:
            return h
        if self.reverse_enabled:
            if ip not in self._reverse:
                self._reverse[ip] = None
                self._pool.submit(self._lookup_reverse, ip)
            return self._reverse.get(ip) or ""
        return ""

    def _lookup_reverse(self, ip):
        try:
            name = socket.gethostbyaddr(ip)[0]
            if name and name != ip:
                self._reverse[ip] = name
        except OSError:
            pass

    def _name(self, pid):
        name, exe = self.names.get(pid)
        if not name.startswith("(終了済み"):
            self.pid_names[pid] = name
        return name, exe

    # ----- 1 秒ごとの採取 -----
    def _loop(self):
        while True:
            time.sleep(self.interval)
            try:
                self.sample()
            except Exception as e:  # 採取失敗でスレッドを止めない
                print("net error:", e)

    def sample(self):
        now = time.time()
        dt = max(now - self._prev_t, 0.2)
        self._prev_t = now
        try:
            conns = psutil.net_connections(kind="inet")
        except (psutil.AccessDenied, OSError):
            conns = []
        deltas = self.agent.take()
        names = _Names(self)

        with self.lock:
            seen = set()
            # 1) OS の接続テーブル
            for c in conns:
                pid = c.pid or 0
                tcp = c.type == socket.SOCK_STREAM
                la = norm_ip(c.laddr.ip) if c.laddr else ""
                lp = c.laddr.port if c.laddr else 0
                ra = norm_ip(c.raddr.ip) if (tcp and c.raddr) else ""
                rp = c.raddr.port if (tcp and c.raddr) else 0
                state = (c.status or "") if tcp else ""
                if tcp and state not in ("LISTEN", "TIME_WAIT", "NONE") and ra:
                    self.recorder.observe(pid, "TCP", la, lp, ra, rp, names)
                    self.stats.observe(pid, la, lp, ra, rp)
                key = f"T|{pid}|{la}|{lp}|{ra}|{rp}" if tcp else f"U|{pid}|{lp}"
                row = self._rows.get(key) or self._new_row(key, "TCP" if tcp else "UDP", pid, la, lp, ra, rp, now)
                row["state"] = state
                row["listen"] = state == "LISTEN"
                row["timewait"] = state == "TIME_WAIT"
                seen.add(key)

            # 2) ETW の通信量（取得の合間に開いて閉じた短命な接続もここで拾う）
            touched = set()
            for (proto, pid, la, lp, ra, rp), (s, r) in deltas.items():
                pr = "TCP" if proto == "T" else "UDP"
                self.recorder.add_bytes(pid, pr, la, lp, ra, rp, s, r, names)
                self.stats.add_bytes(pid, pr, la, lp, ra, rp, s, r)
                key = f"T|{pid}|{la}|{lp}|{ra}|{rp}" if proto == "T" else f"U|{pid}|{lp}"
                row = self._rows.get(key)
                if row is None:
                    if pr == "TCP" and is_loopback(ra):
                        continue
                    row = self._new_row(key, pr, pid, la if pr == "TCP" else "*", lp, ra if pr == "TCP" else "", rp if pr == "TCP" else 0, now)
                    row["state"] = SHORT_LIVED
                if pr == "UDP":
                    row["remote"] = endpoint(ra, rp)
                    row["udp_remote_ip"] = ra
                row["_last_traffic"] = now
                row["sent"] += s
                row["recv"] += r
                row["_ds"] = row.get("_ds", 0) + s
                row["_dr"] = row.get("_dr", 0) + r
                touched.add(key)
                pb = self._proc_bytes.setdefault(pid, [0, 0])
                pb[0] += s
                pb[1] += r

            # 3) 速度・新規・終了・ホスト名
            for key in list(self._rows):
                row = self._rows[key]
                # 接続テーブルに出ない短命な接続は、直近 3 秒以内に通信があれば開いているとみなす
                if row["state"] == SHORT_LIVED and now - row.get("_last_traffic", 0) < 3.0:
                    seen.add(key)
                if key in touched:
                    row["up"], row["down"] = row.pop("_ds", 0) / dt, row.pop("_dr", 0) / dt
                else:
                    row["up"] = row["down"] = 0.0
                if key in seen:
                    row["closed"] = False
                    row["closed_at"] = None
                else:
                    if not row["closed"]:
                        row["closed"], row["closed_at"] = True, now
                        if row["state"] != SHORT_LIVED:
                            row["state"] = "閉じた"
                    if now - row["closed_at"] > CLOSED_LINGER:
                        del self._rows[key]
                        continue
                row["new"] = (now - row["first"]) < NEW_HIGHLIGHT and not row["initial"]
                ip = row["raddr"] or row.get("udp_remote_ip", "")
                if ip and not row["host"]:
                    row["host"] = self.host_of(ip)

            # 4) プロセス別
            procs = {}
            for row in self._rows.values():
                if row["closed"]:
                    continue
                p = procs.setdefault(row["pid"], {"pid": row["pid"], "name": row["proc"], "path": row["path"],
                                                  "conns": 0, "remotes": set(), "up": 0.0, "down": 0.0})
                if not (row["listen"] or row["timewait"]):
                    p["conns"] += 1
                if row["raddr"] and not is_unspecified(row["raddr"]):
                    p["remotes"].add(row["raddr"])
                p["up"] += row["up"]
                p["down"] += row["down"]
            plist = []
            for p in procs.values():
                b = self._proc_bytes.get(p["pid"], [0, 0])
                p["remotes"] = len(p["remotes"])
                p["sent"], p["recv"] = b
                plist.append(p)
            for pid in list(self._proc_bytes):
                if pid not in procs:
                    del self._proc_bytes[pid]

            rows = [{k: v for k, v in r.items() if not k.startswith("_") and k not in ("closed_at", "initial", "udp_remote_ip")}
                    for r in self._rows.values()]
            self._latest = {"time": now, "rows": rows, "procs": plist}
            self._first = False

    def _new_row(self, key, proto, pid, la, lp, ra, rp, now):
        name, exe = self._name(pid)
        row = {"key": key, "proto": proto, "pid": pid, "proc": name, "path": exe,
               "laddr": la or "*", "lport": lp, "raddr": ra, "rport": rp,
               "local": endpoint(la or "*", lp) if la != "*" else f"*:{lp}",
               "remote": endpoint(ra, rp) if ra and not is_unspecified(ra) else ("*" if proto == "TCP" else ""),
               "host": "", "state": "", "sent": 0, "recv": 0, "up": 0.0, "down": 0.0,
               "first": now, "closed": False, "closed_at": None, "new": False, "initial": self._first,
               "loopback": is_loopback(ra) if proto == "TCP" else is_loopback(la),
               "listen": False, "timewait": False}
        self._rows[key] = row
        return row

    # ----- API 用 -----
    def snapshot(self):
        with self.lock:
            d = dict(self._latest)
        d["agent"] = dict(self.agent.state)
        d["admin"] = self.admin
        d["record"] = self.recorder.status()
        d["settings"] = self.settings
        return d

    def countries(self, metric="bytes", include_local=False):
        aggs = {}
        for ip, sent, recv, conns, pids in self.stats.snapshot():
            iso, name = self.geo.lookup(ip)
            if iso == "LAN" and not include_local:
                continue
            a = aggs.setdefault(iso, {"iso": iso, "name": name, "sent": 0, "recv": 0, "conns": 0, "ips": 0, "procs": set()})
            a["sent"] += sent
            a["recv"] += recv
            a["conns"] += conns
            a["ips"] += 1
            a["procs"].update(self.pid_names.get(p, f"PID {p}") for p in pids)

        def val(a):
            return a["sent"] + a["recv"] if metric == "bytes" else a["conns"] if metric == "connections" else a["ips"]

        total = sum(val(a) for a in aggs.values()) or 0
        rows = []
        for a in aggs.values():
            v = val(a)
            procs = sorted(a["procs"], key=str.lower)
            rows.append({**a, "value": v, "share": (v / total) if total else 0, "procs": procs})
        rows.sort(key=lambda r: r["value"], reverse=True)
        top = next((r for r in rows if r["value"] > 0 and r["iso"] not in ("LAN", "??")), None)
        return {"metric": metric, "rows": rows, "total": total, "max": rows[0]["value"] if rows else 0,
                "top": top, "since": self.stats.since.strftime("%m/%d %H:%M"),
                "ip_count": sum(r["ips"] for r in rows),
                "db": {"build": self.geo.build_date, "error": self.geo.error}}

    def country_ips(self, iso, include_local=False):
        out = []
        for ip, sent, recv, conns, pids in self.stats.snapshot():
            if self.geo.lookup(ip)[0] != iso:
                continue
            out.append({"ip": ip, "host": self.host_of(ip), "sent": sent, "recv": recv, "conns": conns,
                        "procs": sorted({self.pid_names.get(p, f"PID {p}") for p in pids}, key=str.lower)})
        out.sort(key=lambda r: (-(r["sent"] + r["recv"]), -r["conns"]))
        return out

    def close(self):
        self.agent.stop()


class _Names:
    """記録用：最初に見つけたときにプロセス名を引く（すぐ終了するプロセスでも名前が残るように）"""

    def __init__(self, mon):
        self.mon = mon

    def get(self, pid):
        return self.mon._name(pid)
