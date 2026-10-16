"""
ファイル・フォルダの一覧と削除（ストレージ画面用）

安全のための方針
- 既定は「ごみ箱へ移動」（Send2Trash）。完全削除は呼び出し側で明示したときだけ
- OS やアプリの動作に必要な場所、ドライブ直下、ユーザーフォルダ本体などは削除を拒否する
  （それらの「親」にあたるフォルダも拒否。例: C:\\Users を消すとユーザーフォルダごと消えるため）
"""
import os
import shutil
import stat
import sys
from datetime import datetime


def _norm(p):
    return os.path.normcase(os.path.abspath(p)).rstrip("\\/") or os.sep


def _is_same_or_inside(path, parent):
    """path が parent と同じ、または parent の中にあるか"""
    path, parent = _norm(path), _norm(parent)
    if path == parent:
        return True
    return path.startswith(parent.rstrip("\\/") + os.sep)


def _protected_lists(extra_keep=()):
    """
    deny_inside: この中にあるものは削除不可（フォルダ自体も不可）
    keep:        これ自体と、これを含む親フォルダは削除不可（中身は削除可）
    """
    env = os.environ.get
    deny_inside, keep = [], list(extra_keep)
    if sys.platform == "win32":
        sysdrive = (env("SystemDrive") or "C:") + "\\"
        for p in (env("SystemRoot") or env("WINDIR") or sysdrive + "Windows",
                  env("ProgramFiles"), env("ProgramFiles(x86)"), env("ProgramW6432"),
                  env("ProgramData"),
                  sysdrive + "System Volume Information", sysdrive + "$Recycle.Bin",
                  sysdrive + "Recovery", sysdrive + "Boot", sysdrive + "EFI",
                  sysdrive + "pagefile.sys", sysdrive + "hiberfil.sys", sysdrive + "swapfile.sys",
                  sysdrive + "bootmgr", sysdrive + "Config.Msi"):
            if p:
                deny_inside.append(p)
        for p in (env("USERPROFILE"), env("APPDATA"), env("LOCALAPPDATA"), env("PUBLIC"),
                  os.path.join(env("USERPROFILE") or "", "Documents"),
                  os.path.join(env("USERPROFILE") or "", "Desktop"),
                  os.path.join(env("USERPROFILE") or "", "Downloads"),
                  os.path.join(env("USERPROFILE") or "", "OneDrive")):
            if p and os.path.isabs(p):
                keep.append(p)
    else:
        for p in ("/bin", "/boot", "/dev", "/etc", "/lib", "/lib64", "/proc", "/sbin",
                  "/sys", "/usr", "/var", "/opt", "/snap", "/System", "/Library", "/Applications"):
            deny_inside.append(p)
        keep.append(os.path.expanduser("~"))
    return deny_inside, keep


def check_deletable(path, extra_keep=()):
    """削除してよいか。(ok, 理由)"""
    if not path or not os.path.isabs(path):
        return False, "絶対パスではありません"
    if not os.path.lexists(path):
        return False, "見つかりません（すでに削除済みの可能性があります）"
    n = _norm(path)
    drive, rest = os.path.splitdrive(n)
    if rest.strip("\\/") == "":
        return False, "ドライブ直下・ルートは削除できません"
    deny_inside, keep = _protected_lists(extra_keep)
    for d in deny_inside:
        if _is_same_or_inside(n, d):
            return False, f"システムの保護領域のため削除できません（{d}）"
    for k in keep:
        if _is_same_or_inside(k, n):  # n が k 自身か、k を含む親
            return False, f"重要なフォルダ（またはその親）のため削除できません（{k}）"
    return True, ""


def path_size(path):
    """削除前に容量を数える（ごみ箱に入れたときの解放量の表示用）"""
    try:
        if os.path.islink(path) or not os.path.isdir(path):
            return os.lstat(path).st_size
    except OSError:
        return 0
    total = 0
    for root, dirs, files in os.walk(path, onerror=lambda e: None):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def _rm_readonly(func, p, *_):
    """読み取り専用属性が付いていても完全削除できるようにする"""
    try:
        os.chmod(p, stat.S_IWRITE)
        func(p)
    except Exception:
        raise


def delete_paths(paths, mode="recycle", extra_keep=()):
    """
    mode: "recycle"（ごみ箱へ移動）または "permanent"（完全に削除）
    戻り値: [{"path", "ok", "size", "error"}]
    """
    results = []
    for p in paths:
        r = {"path": p, "ok": False, "size": 0, "error": ""}
        ok, why = check_deletable(p, extra_keep)
        if not ok:
            r["error"] = why
            results.append(r)
            continue
        r["size"] = path_size(p)
        try:
            if mode == "permanent":
                if os.path.isdir(p) and not os.path.islink(p):
                    if sys.version_info >= (3, 12):
                        shutil.rmtree(p, onexc=_rm_readonly)
                    else:
                        shutil.rmtree(p, onerror=_rm_readonly)
                else:
                    try:
                        os.remove(p)
                    except PermissionError:
                        os.chmod(p, stat.S_IWRITE)
                        os.remove(p)
            else:
                from send2trash import send2trash
                send2trash(p)
            r["ok"] = not os.path.lexists(p)
            if not r["ok"]:
                r["error"] = "一部を削除できませんでした（使用中か、権限がありません）"
        except PermissionError:
            r["error"] = "権限がないか、ほかのアプリが使用中です"
        except Exception as e:
            r["error"] = str(e) or e.__class__.__name__
        results.append(r)
    return results


def list_files(path, limit=500):
    """フォルダ直下のファイル（サイズの大きい順）。サブフォルダは含めない"""
    out, total = [], 0
    with os.scandir(path) as it:
        for e in it:
            try:
                if e.is_dir(follow_symlinks=False):
                    continue
                st = e.stat(follow_symlinks=False)
                total += 1
                out.append({"name": e.name, "path": e.path, "size": st.st_size,
                            "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")})
            except OSError:
                continue
    out.sort(key=lambda f: f["size"], reverse=True)
    return {"path": path, "files": out[:limit], "total": total, "truncated": total > limit}


def reveal(path):
    """エクスプローラーで場所を開く（ファイルなら選択状態で）"""
    import subprocess
    if sys.platform == "win32":
        if os.path.isdir(path):
            subprocess.Popen(["explorer", os.path.normpath(path)])
        else:
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        return True
    return False
