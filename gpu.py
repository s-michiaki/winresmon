"""
GPU 監視（Windows）

- アダプター一覧（名前・VRAM 容量・LUID）: DXGI（ctypes で直接呼び出し）
- 使用率（エンジン別）・VRAM 使用量・プロセス別使用量: Windows パフォーマンスカウンター
  （タスクマネージャーと同じ "GPU Engine" / "GPU Adapter Memory" / "GPU Process Memory"）
  → NVIDIA / AMD / Intel を問わず取得できる。管理者権限は不要
- 温度・消費電力・クロック・ファン: NVIDIA のみ NVML（ドライバー同梱の nvml.dll）から取得

どれかが使えない環境でも例外は外に出さず、取れた範囲だけを返す。
"""
import os
import re
import sys
import time

ENGINE_RE = re.compile(
    r"pid_(\d+)_luid_(0x[0-9a-f]+)_(0x[0-9a-f]+)_phys_(\d+)_eng_(\d+)_engtype_(.*)$", re.I)
ADAPTER_RE = re.compile(r"luid_(0x[0-9a-f]+)_(0x[0-9a-f]+)_phys_(\d+)", re.I)
PROCMEM_RE = re.compile(r"pid_(\d+)_luid_(0x[0-9a-f]+)_(0x[0-9a-f]+)_phys_(\d+)", re.I)

VENDORS = {0x10DE: "NVIDIA", 0x1002: "AMD", 0x1022: "AMD", 0x8086: "Intel",
           0x1414: "Microsoft", 0x5143: "Qualcomm"}


def _luid_key(high, low):
    """LUID を 'hhhhhhhh_llllllll'（小文字16進）に正規化"""
    return f"{int(str(high), 0) & 0xFFFFFFFF:08x}_{int(str(low), 0) & 0xFFFFFFFF:08x}"


def _engine_group(engtype):
    """エンジン種別をまとめる（Compute_0 / Compute_1 → Compute など）"""
    t = engtype.strip()
    for base in ("Compute", "Graphics", "VideoDecode", "VideoEncode", "VideoProcessing", "Copy"):
        if t.lower().startswith(base.lower()):
            return base
    return t or "Other"


# ---------------------------------------------------------------------------
# DXGI: アダプター一覧
# ---------------------------------------------------------------------------
def list_adapters_dxgi(include_software=False):
    if sys.platform != "win32":
        return []
    import ctypes
    import uuid
    from ctypes import wintypes

    class LUID(ctypes.Structure):
        _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]

    class DXGI_ADAPTER_DESC1(ctypes.Structure):
        _fields_ = [("Description", wintypes.WCHAR * 128),
                    ("VendorId", wintypes.UINT), ("DeviceId", wintypes.UINT),
                    ("SubSysId", wintypes.UINT), ("Revision", wintypes.UINT),
                    ("DedicatedVideoMemory", ctypes.c_size_t),
                    ("DedicatedSystemMemory", ctypes.c_size_t),
                    ("SharedSystemMemory", ctypes.c_size_t),
                    ("AdapterLuid", LUID), ("Flags", wintypes.UINT)]

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    u = uuid.UUID("770aae78-f26f-4dba-a829-253c83d1b387")  # IID_IDXGIFactory1
    iid = GUID(u.time_low, u.time_mid, u.time_hi_version, (ctypes.c_ubyte * 8)(*u.bytes[8:]))

    def vfunc(obj, index, *argtypes, restype=ctypes.c_long):
        vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
        return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtbl[index])

    factory = ctypes.c_void_p()
    hr = ctypes.windll.dxgi.CreateDXGIFactory1(ctypes.byref(iid), ctypes.byref(factory))
    if hr != 0 or not factory:
        raise OSError(f"CreateDXGIFactory1 failed hr=0x{hr & 0xFFFFFFFF:08X}")

    out = []
    try:
        enum1 = vfunc(factory, 12, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p))  # EnumAdapters1
        i = 0
        while i < 16:
            adapter = ctypes.c_void_p()
            hr = enum1(factory, i, ctypes.byref(adapter))
            if hr != 0:  # DXGI_ERROR_NOT_FOUND で列挙終了
                break
            try:
                desc = DXGI_ADAPTER_DESC1()
                get_desc1 = vfunc(adapter, 10, ctypes.POINTER(DXGI_ADAPTER_DESC1))
                if get_desc1(adapter, ctypes.byref(desc)) == 0:
                    software = bool(desc.Flags & 2)  # DXGI_ADAPTER_FLAG_SOFTWARE
                    if include_software or not software:
                        out.append({
                            "name": desc.Description.strip(),
                            "vendor": VENDORS.get(desc.VendorId, f"0x{desc.VendorId:04X}"),
                            "vendor_id": desc.VendorId,
                            "dedicated_total": int(desc.DedicatedVideoMemory),
                            "shared_total": int(desc.SharedSystemMemory),
                            "luid": _luid_key(desc.AdapterLuid.HighPart, desc.AdapterLuid.LowPart),
                            "software": software,
                        })
            finally:
                vfunc(adapter, 2, restype=ctypes.c_ulong)(adapter)  # Release
            i += 1
    finally:
        vfunc(factory, 2, restype=ctypes.c_ulong)(factory)
    return out


# ---------------------------------------------------------------------------
# パフォーマンスカウンター（PDH）
# ---------------------------------------------------------------------------
class _Pdh:
    PATHS = {
        "engine": r"\GPU Engine(*)\Utilization Percentage",
        "ad_ded": r"\GPU Adapter Memory(*)\Dedicated Usage",
        "ad_shr": r"\GPU Adapter Memory(*)\Shared Usage",
        "pr_ded": r"\GPU Process Memory(*)\Dedicated Usage",
    }

    def __init__(self):
        import win32pdh
        self.p = win32pdh
        self.q = win32pdh.OpenQuery()
        add = getattr(win32pdh, "AddEnglishCounter", win32pdh.AddCounter)
        self.c = {}
        for k, path in self.PATHS.items():
            try:
                self.c[k] = add(self.q, path)
            except Exception:
                pass  # 古い Windows 等でカウンターが無い
        if "engine" not in self.c:
            raise OSError("GPU Engine カウンターがありません（Windows 10 1709 以降が必要）")
        win32pdh.CollectQueryData(self.q)  # 使用率は2回目の採取から有効

    def sample(self):
        self.p.CollectQueryData(self.q)
        out = {}
        for k, h in self.c.items():
            try:
                out[k] = self.p.GetFormattedCounterArray(h, self.p.PDH_FMT_DOUBLE)
            except Exception:
                out[k] = {}
        return out


# ---------------------------------------------------------------------------
# NVML（NVIDIA のみ）
# ---------------------------------------------------------------------------
class _Nvml:
    def __init__(self):
        import pynvml as n
        n.nvmlInit()
        self.n = n
        self.devs = []
        for i in range(n.nvmlDeviceGetCount()):
            h = n.nvmlDeviceGetHandleByIndex(i)
            name = n.nvmlDeviceGetName(h)
            if isinstance(name, bytes):
                name = name.decode(errors="ignore")
            self.devs.append({"name": name, "h": h})

    def read(self, h):
        n = self.n
        d = {}

        def tryget(key, fn):
            try:
                d[key] = fn()
            except Exception:
                pass
        tryget("temp_c", lambda: n.nvmlDeviceGetTemperature(h, n.NVML_TEMPERATURE_GPU))
        tryget("power_w", lambda: n.nvmlDeviceGetPowerUsage(h) / 1000.0)
        tryget("power_limit_w", lambda: n.nvmlDeviceGetEnforcedPowerLimit(h) / 1000.0)
        tryget("clock_core_mhz", lambda: n.nvmlDeviceGetClockInfo(h, n.NVML_CLOCK_GRAPHICS))
        tryget("clock_mem_mhz", lambda: n.nvmlDeviceGetClockInfo(h, n.NVML_CLOCK_MEM))
        tryget("fan_pct", lambda: n.nvmlDeviceGetFanSpeed(h))
        tryget("nv_util", lambda: n.nvmlDeviceGetUtilizationRates(h).gpu)
        try:
            m = n.nvmlDeviceGetMemoryInfo(h)
            d["nv_mem_used"], d["nv_mem_total"] = int(m.used), int(m.total)
        except Exception:
            pass
        return d


# ---------------------------------------------------------------------------
# まとめ役
# ---------------------------------------------------------------------------
class GpuMonitor:
    def __init__(self):
        self.adapters = []
        self.errors = []
        self.pdh = None
        self.nvml = None
        self.fake = os.environ.get("WINRESMON_FAKE_GPU") == "1"  # 開発用の疑似データ
        self._t0 = time.time()
        if self.fake:
            self.adapters = [
                {"name": "NVIDIA GeForce RTX 4070 (疑似)", "vendor": "NVIDIA", "vendor_id": 0x10DE,
                 "dedicated_total": 12 << 30, "shared_total": 16 << 30, "luid": "00000000_0000a1b2", "software": False},
                {"name": "Intel(R) UHD Graphics 770 (疑似)", "vendor": "Intel", "vendor_id": 0x8086,
                 "dedicated_total": 128 << 20, "shared_total": 16 << 30, "luid": "00000000_0000c3d4", "software": False},
            ]
            return
        if sys.platform != "win32":
            return
        try:
            self.adapters = list_adapters_dxgi()
        except Exception as e:
            self.errors.append(f"DXGI: {e}")
        if not self.adapters:
            return
        try:
            self.pdh = _Pdh()
        except Exception as e:
            self.errors.append(f"PDH: {e}")
        if any(a["vendor_id"] == 0x10DE for a in self.adapters):
            try:
                self.nvml = _Nvml()
            except Exception as e:
                self.errors.append(f"NVML: {e}")
        # NVML デバイスを名前でアダプターに対応付け（同名が複数あれば順番に）
        if self.nvml:
            pool = list(self.nvml.devs)
            for a in self.adapters:
                for d in pool:
                    if d["name"].lower() in a["name"].lower() or a["name"].lower() in d["name"].lower():
                        a["_nvml"] = d["h"]
                        pool.remove(d)
                        break

    @property
    def available(self):
        return bool(self.adapters)

    def sample(self, proc_name=lambda pid: str(pid)):
        """戻り値: (gpus, processes)"""
        if self.fake:
            return self._fake_sample()
        if not self.adapters:
            return [], []

        raw = {}
        if self.pdh:
            try:
                raw = self.pdh.sample()
            except Exception as e:
                self.errors.append(f"PDH sample: {e}")
                raw = {}

        # エンジン別：(luid, eng番号) ごとに全プロセスの合計 → 種別ごとに最大（タスクマネージャーと同じ考え方）
        eng_sum = {}      # (luid, eng) -> util
        eng_type = {}     # (luid, eng) -> group
        proc_eng = {}     # (pid, luid, eng) -> util
        for inst, v in raw.get("engine", {}).items():
            m = ENGINE_RE.search(inst)
            if not m or v is None:
                continue
            pid, hi, lo, _phys, eng, et = m.groups()
            luid = _luid_key(hi, lo)
            key = (luid, int(eng))
            eng_sum[key] = eng_sum.get(key, 0.0) + v
            eng_type[key] = _engine_group(et)
            pk = (int(pid), luid, int(eng))
            proc_eng[pk] = proc_eng.get(pk, 0.0) + v

        ad_ded, ad_shr = {}, {}
        for src, dst in (("ad_ded", ad_ded), ("ad_shr", ad_shr)):
            for inst, v in raw.get(src, {}).items():
                m = ADAPTER_RE.search(inst)
                if m:
                    luid = _luid_key(m.group(1), m.group(2))
                    dst[luid] = dst.get(luid, 0) + int(v or 0)

        gpus = []
        for a in self.adapters:
            luid = a["luid"]
            engines = {}
            for (l, e), u in eng_sum.items():
                if l == luid:
                    g = eng_type[(l, e)]
                    engines[g] = max(engines.get(g, 0.0), min(u, 100.0))
            g = {
                "name": a["name"], "vendor": a["vendor"], "luid": luid,
                "util": max(engines.values()) if engines else None,
                "engines": engines,
                "dedicated_used": ad_ded.get(luid), "dedicated_total": a["dedicated_total"],
                "shared_used": ad_shr.get(luid), "shared_total": a["shared_total"],
            }
            if a.get("_nvml") is not None:
                nv = self.nvml.read(a["_nvml"])
                g.update({k: v for k, v in nv.items() if not k.startswith("nv_")})
                if g["util"] is None and "nv_util" in nv:
                    g["util"] = float(nv["nv_util"])
                if g["dedicated_used"] is None and "nv_mem_used" in nv:
                    g["dedicated_used"] = nv["nv_mem_used"]
            gpus.append(g)

        # プロセス別：使用率は各エンジンのうち最大、VRAM は専用メモリ使用量
        names = {a["luid"]: a["name"] for a in self.adapters}
        procs = {}
        for (pid, luid, _e), u in proc_eng.items():
            if luid not in names:
                continue
            p = procs.setdefault((pid, luid), {"pid": pid, "gpu": names[luid], "util": 0.0, "vram": 0})
            p["util"] = max(p["util"], min(u, 100.0))
        for inst, v in raw.get("pr_ded", {}).items():
            m = PROCMEM_RE.search(inst)
            if not m:
                continue
            pid, luid = int(m.group(1)), _luid_key(m.group(2), m.group(3))
            if luid not in names or not v:
                continue
            p = procs.setdefault((pid, luid), {"pid": pid, "gpu": names[luid], "util": 0.0, "vram": 0})
            p["vram"] += int(v)
        plist = [p for p in procs.values() if p["pid"] != 0 and (p["util"] > 0.05 or p["vram"] > 0)]
        plist.sort(key=lambda p: (p["util"], p["vram"]), reverse=True)
        plist = plist[:10]
        for p in plist:
            p["name"] = proc_name(p["pid"])
        return gpus, plist

    def debug(self):
        """自己テスト用の診断情報"""
        info = {"adapters": [{k: v for k, v in a.items() if not k.startswith("_")} for a in self.adapters],
                "pdh": self.pdh is not None, "nvml": self.nvml is not None, "errors": list(self.errors)}
        if sys.platform == "win32":
            try:
                info["all_adapters_incl_software"] = [a["name"] for a in list_adapters_dxgi(True)]
            except Exception as e:
                info["all_adapters_incl_software"] = f"error: {e}"
            try:
                p = _Pdh()
                time.sleep(0.5)
                s = p.sample()
                info["pdh_instances"] = {k: len(v) for k, v in s.items()}
                info["pdh_engine_example"] = next(iter(s.get("engine", {})), None)
            except Exception as e:
                info["pdh_instances"] = f"error: {e}"
        return info

    # ---- 開発用 ----
    def _fake_sample(self):
        import math
        t = time.time() - self._t0
        u1 = 45 + 35 * math.sin(t / 6)
        gpus = [
            {"name": self.adapters[0]["name"], "vendor": "NVIDIA", "luid": self.adapters[0]["luid"],
             "util": u1, "engines": {"3D": u1, "Copy": 8.0, "VideoDecode": 22.0, "VideoEncode": 0.0, "Compute": u1 * 0.4},
             "dedicated_used": int((5.2 + math.sin(t / 9)) * (1 << 30)), "dedicated_total": 12 << 30,
             "shared_used": 310 << 20, "shared_total": 16 << 30,
             "temp_c": 58 + u1 / 5, "power_w": 60 + u1 * 1.4, "power_limit_w": 200.0,
             "clock_core_mhz": 2475, "clock_mem_mhz": 10501, "fan_pct": 38},
            {"name": self.adapters[1]["name"], "vendor": "Intel", "luid": self.adapters[1]["luid"],
             "util": 6.0, "engines": {"3D": 6.0, "VideoProcessing": 3.0},
             "dedicated_used": 0, "dedicated_total": 128 << 20, "shared_used": 900 << 20, "shared_total": 16 << 30},
        ]
        procs = [
            {"pid": 4120, "name": "Game.exe", "gpu": gpus[0]["name"], "util": u1 * 0.8, "vram": 3_900 << 20},
            {"pid": 7712, "name": "chrome.exe", "gpu": gpus[0]["name"], "util": 7.5, "vram": 620 << 20},
            {"pid": 1440, "name": "dwm.exe", "gpu": gpus[1]["name"], "util": 4.1, "vram": 0},
        ]
        return gpus, procs
