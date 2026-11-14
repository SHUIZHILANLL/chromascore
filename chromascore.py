#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
ChromaScore / 音色谱 — 本地音乐色彩播放器
=========================================
扫描本地曲库，起一个本地服务，浏览器里就是播放器。

    python chromascore.py "D:\\Music"
    python chromascore.py "D:\\Music" --port 8770
    （或者把文件夹拖到「音色谱.bat」上）

要点
  · 曲库是扫描出来的，音频不内嵌，多少首都行
  · 封面读 mp3/flac/m4a 内嵌图，没有就找同目录 cover.jpg
  · 情绪分析在后台并行跑，结果缓存，第二次启动秒开
"""

from __future__ import annotations

import base64
import hashlib
import html
import io
import json
import math
import mimetypes
import os
import re
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import numpy as np

# --------------------------------------------------------------------------- #
AUDIO_EXT = (".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aac", ".aiff", ".wma", ".opus")
MIME = {".mp3": "audio/mpeg", ".wav": "audio/wav", ".flac": "audio/flac",
        ".m4a": "audio/mp4", ".ogg": "audio/ogg", ".aac": "audio/aac",
        ".aiff": "audio/aiff", ".wma": "audio/x-ms-wma", ".opus": "audio/ogg"}
COVER_NAMES = ("cover.jpg", "cover.jpeg", "cover.png", "folder.jpg",
               "folder.png", "front.jpg", "front.png", "album.jpg")

MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])
PITCH = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

HUE_ANCHOR = [(45.0, 35.0), (135.0, 320.0), (225.0, 255.0), (315.0, 165.0)]
QCOLOR = {"明亮激昂": "#e8a04f", "温暖平静": "#5fd6a8",
          "紧张暗涌": "#d468a8", "阴郁低沉": "#7b8fd0"}

WIN_SEC = 4.0
STATE = {"tracks": [], "lock": threading.Lock(), "done": 0, "total": 0, "running": False}


# --------------------------------------------------------------------------- #
# 运行环境 / 配置
# --------------------------------------------------------------------------- #
def is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def app_dir() -> str:
    """打包后 __file__ 在临时目录，缓存和配置必须放到用户目录。"""
    if is_frozen():
        base = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")),
                            "ChromaScore")
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    try:
        os.makedirs(base, exist_ok=True)
    except Exception:
        base = os.path.expanduser("~")
    return base


def cfg_path() -> str:
    return os.path.join(app_dir(), "config.json")


def load_config() -> dict:
    # 用 utf-8-sig：手写或记事本保存的 json 常带 BOM，普通 utf-8 会解析失败
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            with open(cfg_path(), "r", encoding=enc) as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception:
            continue
    return {}


def save_config(c: dict) -> None:
    try:
        with open(cfg_path(), "w", encoding="utf-8") as f:
            json.dump(c, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


def open_app_window(url: str) -> bool:
    """用 Edge/Chrome 的 --app 模式打开，看起来就是个独立播放器，没有地址栏。"""
    for exe in BROWSERS:
        if os.path.exists(exe):
            try:
                subprocess.Popen([exe, f"--app={url}", "--window-size=1320,880",
                                  "--user-data-dir=" + os.path.join(app_dir(), "ui")])
                return True
            except Exception:
                continue
    try:
        webbrowser.open(url)
    except Exception:
        pass
    return False


# --------------------------------------------------------------------------- #
# 色彩
# --------------------------------------------------------------------------- #
def oklch_to_srgb(l: float, c: float, h: float):
    a = c * math.cos(math.radians(h))
    b = c * math.sin(math.radians(h))
    l_ = l + 0.3963377774 * a + 0.2158037573 * b
    m_ = l - 0.1055613458 * a - 0.0638541728 * b
    s_ = l - 0.0894841775 * a - 1.2914855480 * b
    l3, m3, s3 = l_ ** 3, m_ ** 3, s_ ** 3
    r = 4.0767416621 * l3 - 3.3077115913 * m3 + 0.2309699292 * s3
    g = -1.2684380046 * l3 + 2.6097574011 * m3 - 0.3413193965 * s3
    bb = -0.0041960863 * l3 - 0.7034186147 * m3 + 1.7076147010 * s3

    def enc(x):
        x = min(max(x, 0.0), 1.0)
        return x * 12.92 if x <= 0.0031308 else 1.055 * (x ** (1 / 2.4)) - 0.055

    return tuple(int(round(enc(v) * 255)) for v in (r, g, bb))


def hue_for(theta_deg: float) -> float:
    t = theta_deg % 360.0
    for i in range(4):
        a0, h0 = HUE_ANCHOR[i]
        a1, h1 = HUE_ANCHOR[(i + 1) % 4]
        span = a1 - a0 if a1 > a0 else a1 - a0 + 360.0
        d = (t - a0) % 360.0
        if d <= span:
            return (h0 + (h1 - h0) * (d / span)) % 360.0
    return 35.0


def va_hex(v: float, a: float, l0: float = 0.46, l1: float = 0.40) -> str:
    th = math.degrees(math.atan2(a - 0.5, v - 0.5))
    rad = min(math.hypot(v - 0.5, a - 0.5) / 0.7071, 1.0)
    c = 0.055 + 0.24 * rad ** 0.75
    return "#%02x%02x%02x" % oklch_to_srgb(l0 + l1 * v, c, hue_for(th))


def quadrant(v: float, a: float) -> str:
    if v >= 0.5 and a >= 0.5:
        return "明亮激昂"
    if v >= 0.5:
        return "温暖平静"
    if a >= 0.5:
        return "紧张暗涌"
    return "阴郁低沉"


def mmss(t: float) -> str:
    return f"{int(t // 60)}:{int(t % 60):02d}"


# --------------------------------------------------------------------------- #
# 扫描 + 元数据 + 封面
# --------------------------------------------------------------------------- #
def split_stem(stem: str) -> tuple[str, str]:
    for sep in (" - ", " – ", " — ", " -", " - "):
        if sep in stem:
            a, b = stem.rsplit(sep, 1)
            if a.strip() and b.strip():
                return a.strip(), b.strip()
    return stem, ""


def read_meta(path: str, want_cover: bool = True) -> dict:
    """标题 / 艺术家 / 时长 / 内嵌封面。失败就退回文件名。"""
    stem = os.path.splitext(os.path.basename(path))[0]
    dflt_t, dflt_a = split_stem(stem)
    out = {"title": dflt_t, "artist": dflt_a, "duration": 0.0, "cover": None}
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".mp3":
            from mutagen.id3 import ID3
            from mutagen.mp3 import MP3
            m = MP3(path)
            out["duration"] = float(m.info.length)
            try:
                tags = ID3(path)
                t = tags.get("TIT2")
                a = tags.get("TPE1")
                if t and str(t).strip():
                    out["title"] = str(t).strip()
                if a and str(a).strip():
                    out["artist"] = str(a).strip()
                if want_cover:
                    apics = [k for k in tags.keys() if k.startswith("APIC")]
                    if apics:
                        apic = tags[apics[0]]
                        out["cover"] = (apic.data, apic.mime or "image/jpeg")
            except Exception:
                pass
        elif ext == ".flac":
            from mutagen.flac import FLAC
            m = FLAC(path)
            out["duration"] = float(m.info.length)
            if m.get("title"):
                out["title"] = m["title"][0]
            if m.get("artist"):
                out["artist"] = m["artist"][0]
            if want_cover and m.pictures:
                p = m.pictures[0]
                out["cover"] = (p.data, p.mime or "image/jpeg")
        elif ext == ".m4a":
            from mutagen.mp4 import MP4
            m = MP4(path)
            out["duration"] = float(m.info.length)
            if m.tags:
                if "\xa9nam" in m.tags:
                    out["title"] = m.tags["\xa9nam"][0]
                if "\xa9ART" in m.tags:
                    out["artist"] = m.tags["\xa9ART"][0]
                if want_cover and "covr" in m.tags and m.tags["covr"]:
                    c = m.tags["covr"][0]
                    fmt = getattr(c, "imageformat", None)
                    mime = "image/png" if fmt == 14 else "image/jpeg"
                    out["cover"] = (bytes(c), mime)
        elif ext == ".ogg":
            from mutagen.oggvorbis import OggVorbis
            m = OggVorbis(path)
            out["duration"] = float(m.info.length)
            if m.get("title"):
                out["title"] = m["title"][0]
            if m.get("artist"):
                out["artist"] = m["artist"][0]
    except Exception:
        pass

    if not out["duration"]:
        try:
            import librosa
            out["duration"] = float(librosa.get_duration(path=path))
        except Exception:
            out["duration"] = 0.0

    if want_cover and out["cover"] is None:
        d = os.path.dirname(path)
        for nm in COVER_NAMES:
            p = os.path.join(d, nm)
            if os.path.exists(p):
                try:
                    with open(p, "rb") as f:
                        out["cover"] = (f.read(),
                                        mimetypes.guess_type(p)[0] or "image/jpeg")
                    break
                except Exception:
                    pass
    return out


def fallback_cover(seed: str) -> bytes:
    """没有任何封面时的兜底：8x8 柔和径向渐变，再由文件名散列出色相。"""
    from PIL import Image
    h = hashlib.md5(seed.encode("utf-8")).hexdigest()
    hue = int(h[:6], 16) % 360
    small = Image.new("RGB", (8, 8))
    for y in range(8):
        for x in range(8):
            d = (((x - 3.5) ** 2 + (y - 3.5) ** 2) ** 0.5) / 5.0
            small.putpixel((x, y), oklch_to_srgb(
                0.46 - 0.20 * d, max(0.115 - 0.065 * d, 0.025), (hue + 45 * d) % 360))
    img = small.resize((360, 360), Image.BICUBIC)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=86, optimize=True)
    return buf.getvalue()


COVER_CACHE: dict[str, tuple[bytes, str]] = {}


def cover_bytes(t: dict) -> tuple[bytes, str]:
    """取封面（内嵌图优先），统一缩到 360px 并缓存，避免一次拖动几十张原图。"""
    key = t["path"]
    if key in COVER_CACHE:
        return COVER_CACHE[key]

    m = read_meta(t["path"])
    got = m.get("cover")
    if got is None:
        data, mime = fallback_cover(t["path"]), "image/jpeg"
    else:
        data, mime = got
        try:
            from PIL import Image
            im = Image.open(io.BytesIO(data))
            im = im.convert("RGB")
            im.thumbnail((360, 360), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=86, optimize=True)
            data, mime = buf.getvalue(), "image/jpeg"
        except Exception:
            if not data:
                data, mime = fallback_cover(t["path"]), "image/jpeg"

    COVER_CACHE[key] = (data, mime)
    return data, mime


def scan(root: str) -> list[dict]:
    files = []
    for dirpath, dirnames, names in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for nm in names:
            if nm.lower().endswith(AUDIO_EXT) and not nm.startswith("."):
                files.append(os.path.join(dirpath, nm))
    files.sort(key=lambda p: os.path.basename(p).lower())

    tracks = []
    for i, p in enumerate(files):
        st = os.stat(p)
        m = read_meta(p, want_cover=False)
        tracks.append({
            "i": i,
            "path": p,
            "name": m["title"],
            "artist": m["artist"],
            "duration": m["duration"],
            "size": st.st_size,
            "mtime": int(st.st_mtime),
            "segs": None,
            "dominant": None,
            "tempo": None,
            "key": None,
            "counts": None,
            "verdict": None,
            "color": None,
        })
    return tracks


# --------------------------------------------------------------------------- #
# 分析（带缓存）
# --------------------------------------------------------------------------- #
def cache_key(t: dict) -> str:
    return f"{t['path']}|{t['mtime']}|{t['size']}"


def load_cache(path: str) -> dict:
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_cache(path: str, cache: dict) -> None:
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        pass


def profile_corr(hist, prof):
    if hist.std() < 1e-9:
        return 0.0, 0
    best, shift = -2.0, 0
    for s in range(12):
        r = float(np.corrcoef(hist, np.roll(prof, s))[0, 1])
        if r > best:
            best, shift = r, s
    return best, shift


def fold_tempo(t: float, lo: float = 70.0, hi: float = 140.0) -> float:
    if t <= 0:
        return 100.0
    while t > hi:
        t /= 2.0
    while t < lo:
        t *= 2.0
    return t


def verdict(tempo, mode, dominant, counts, vol, turn, nseg) -> str:
    speed = ("很慢" if tempo < 70 else "偏慢" if tempo < 90 else
             "中速" if tempo < 120 else "偏快" if tempo < 150 else "很快")
    tone = "大调" if mode == "major" else "小调"
    vtxt = ("情绪起伏很大" if vol > 0.13 else
            "情绪有明显推进" if vol > 0.08 else "情绪相当稳定")
    total = max(sum(counts.values()), 1)
    ordered = sorted(counts.items(), key=lambda kv: -kv[1])
    extra = ""
    if len(ordered) > 1 and ordered[1][1] / total >= 0.18:
        extra = f"，其次 {round(ordered[1][1] / total * 100)}% 是「{ordered[1][0]}」"
    turn_txt = f"{mmss(turn)} 附近有明显转折" if vol > 0.08 and nseg > 6 else "全曲走向连贯"
    share = round(counts[dominant] / total * 100)
    lead = "整体偏" if share >= 55 else "整体大致落在"
    return (f"{speed}（{tempo:.0f} BPM）、{tone}，{lead}「{dominant}」"
            f"（{share}%）{extra}；{vtxt}，{turn_txt}。")


def analyze_one(path: str) -> dict:
    import librosa

    y, sr = librosa.load(path, sr=22050, mono=True)
    dur = len(y) / sr
    if dur < 2.0:
        raise ValueError("too short")

    hop = 512
    fps = sr / hop
    oenv = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop)
    tempo = fold_tempo(float(np.atleast_1d(
        librosa.feature.tempo(onset_envelope=oenv, sr=sr, hop_length=hop))[0]))

    rms = librosa.feature.rms(y=y, hop_length=hop)[0]
    cen = librosa.feature.spectral_centroid(y=y, sr=sr, hop_length=hop)[0]
    chroma = librosa.feature.chroma_cqt(y=y, sr=sr, hop_length=hop)

    n = min(len(rms), len(cen), len(oenv), chroma.shape[1])
    rms, cen, oenv, chroma = rms[:n], cen[:n], oenv[:n], chroma[:, :n]

    hist = chroma.mean(axis=1)
    rM, sM = profile_corr(hist, MAJOR)
    rm, sm = profile_corr(hist, MINOR)
    key_name, mode = ((PITCH[sM % 12], "major") if rM >= rm
                      else (PITCH[sm % 12], "minor"))

    w = max(int(WIN_SEC * fps), 1)
    nw = max(n // w, 1)
    modes, rmss, cens, fluxes = [], [], [], []
    for i in range(nw):
        sl = slice(i * w, (i + 1) * w)
        h = chroma[:, sl].mean(axis=1)
        r1, _ = profile_corr(h, MAJOR)
        r2, _ = profile_corr(h, MINOR)
        modes.append(float(np.clip(r1 - r2, -1, 1)))
        rmss.append(float(rms[sl].mean()))
        cens.append(float(cen[sl].mean()))
        fluxes.append(float(oenv[sl].mean()))

    mode_abs = np.clip(0.5 + np.array(modes) * 0.9, 0.05, 0.95)
    bright_abs = np.clip(
        np.log(np.maximum(np.array(cens), 1.0) / 500.0) / np.log(8.0), 0.0, 1.0)
    rms_db = librosa.amplitude_to_db(np.array(rmss), ref=1.0)
    loud_abs = np.clip((rms_db + 45.0) / 45.0, 0.0, 1.0)
    fx = np.array(fluxes)
    mn, mx = float(fx.min()), float(fx.max())
    flux_n = (np.full_like(fx, 0.5) if mx - mn < 1e-9
              else 0.12 + (fx - mn) / (mx - mn) * 0.80)
    speed_abs = float(np.clip((tempo - 70.0) / 80.0, 0.0, 1.0))

    ar = np.clip(0.40 * loud_abs + 0.35 * speed_abs + 0.25 * flux_n - 0.02, 0.03, 0.97)
    va = np.clip(0.62 * mode_abs + 0.26 * bright_abs + 0.12 * (1.0 - loud_abs) - 0.03,
                 0.03, 0.97)

    emax = float(max(rmss, default=1.0)) or 1.0
    segs, counts = [], {}
    for i in range(nw):
        v, a = float(va[i]), float(ar[i])
        q = quadrant(v, a)
        counts[q] = counts.get(q, 0) + 1
        segs.extend([round(v, 3), round(a, 3), round(rmss[i] / emax, 3)])

    d = np.hypot(np.diff(va), np.diff(ar))
    vol = float(d.mean()) if len(d) else 0.0
    turn = float(int(np.argmax(d)) * WIN_SEC) if len(d) else 0.0
    dominant = max(counts, key=counts.get) if counts else "温暖平静"
    mean_hex = va_hex(float(np.mean(va)), float(np.mean(ar)))

    return {
        "segs": segs,
        "dominant": dominant,
        "tempo": round(tempo, 1),
        "key": f"{key_name} {mode}",
        "counts": counts,
        "color": mean_hex,
        "verdict": verdict(tempo, mode, dominant, counts, vol, turn, nw),
    }


def prewarm_covers(n: int = 60) -> None:
    """先把首屏可能看到的封面做出来，避免用户滚动时才现做。"""
    for t in STATE["tracks"][:n]:
        try:
            cover_bytes(t)
        except Exception:
            pass


def analyze_all(root: str, cache_path: str) -> None:
    cache = load_cache(cache_path)
    tracks = STATE["tracks"]
    todo = []
    for t in tracks:
        k = cache_key(t)
        if k in cache:
            t.update(cache[k])
        else:
            todo.append(t)

    with STATE["lock"]:
        STATE["total"] = len(tracks)
        STATE["done"] = len(tracks) - len(todo)
        STATE["running"] = bool(todo)

    # 封面预热排在分析之前，保证首屏立刻好看
    prewarm_covers(60)

    if not todo:
        return

    workers = max(1, min(4, (os.cpu_count() or 4) - 2))
    lock = threading.Lock()
    counter = {"n": 0}

    def job(t):
        try:
            res = analyze_one(t["path"])
        except Exception:
            res = None
        with lock:
            counter["n"] += 1
            if res:
                t.update(res)
                cache[cache_key(t)] = res
            with STATE["lock"]:
                STATE["done"] = len(tracks) - len(todo) + counter["n"]
            if counter["n"] % 8 == 0:
                save_cache(cache_path, cache)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(job, todo))

    save_cache(cache_path, cache)
    with STATE["lock"]:
        STATE["running"] = False
        STATE["done"] = len(tracks)


# --------------------------------------------------------------------------- #
# HTTP 服务
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    server_version = "ChromaScore"

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, data: bytes, ctype: str, code=200,
               cache="public, max-age=86400"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(data)

    def _html(self, text: str):
        """HTML 必须 no-store：否则改完前端，浏览器还捧着磁盘里的旧页面不放。"""
        self._bytes(text.encode("utf-8"), "text/html; charset=utf-8",
                    cache="no-store, must-revalidate")

    def _track(self, i: int):
        tracks = STATE["tracks"]
        if 0 <= i < len(tracks):
            return tracks[i]
        return None

    def do_POST(self):
        u = urlparse(self.path)

        if u.path == "/api/set-library":
            d = ""
            try:
                w = WIN.get("w")
                if w is not None:
                    import webview
                    res = w.create_file_dialog(webview.FOLDER_DIALOG)
                    if res:
                        d = res[0] if isinstance(res, (list, tuple)) else str(res)
            except Exception:
                d = ""
            if not d:
                d = ask_folder()
            if not d or not os.path.isdir(d):
                self._json({"ok": False, "reason": "cancelled"})
                return
            cfg = load_config()
            cfg["library"] = d
            save_config(cfg)
            n = start_scan_analyze(d)
            self._json({"ok": True, "library": d, "tracks": n})
            return

        if u.path == "/api/rescan":
            root = STATE.get("root") or load_config().get("library", "")
            if not root or not os.path.isdir(root):
                self._json({"ok": False, "reason": "no library"}, 400)
                return
            n = start_scan_analyze(root)
            self._json({"ok": True, "tracks": n})
            return

        if u.path == "/api/quit":
            w = WIN.get("w")
            if w is not None:
                threading.Timer(0.25, w.destroy).start()
            self._json({"ok": True})
            return

        if u.path not in ("/shot", "/report"):
            self.send_error(404)
            return
        try:
            n = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(n).decode("ascii", "ignore")
            here = os.path.dirname(os.path.abspath(__file__))
            if u.path == "/shot":
                if "," in raw:
                    raw = raw.split(",", 1)[1]
                data = base64.b64decode(raw)
                with open(os.path.join(here, "shot_canvas.png"), "wb") as f:
                    f.write(data)
                self._json({"ok": True, "bytes": len(data)})
            else:
                with open(os.path.join(here, "diag.json"), "w", encoding="utf-8") as f:
                    f.write(raw)
                self._json({"ok": True})
        except Exception as e:
            self._json({"ok": False, "err": str(e)}, 500)

    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)

        if u.path in ("/", "/index.html", "/viz"):
            body = PAGE
            if u.path == "/viz":
                # 服务端直接给出「初始即可视化」的页面：真实浏览器与无头截图都能拿到同一状态
                body = body.replace('<body class="paused">',
                                    '<body class="paused is-viz">')
            self._html(body)
            return

        if u.path == "/api/status":
            cpath = os.path.join(app_dir(), "cache.json")
            csize = os.path.getsize(cpath) if os.path.exists(cpath) else 0
            with STATE["lock"]:
                done, total, running = STATE["done"], STATE["total"], STATE["running"]
            self._json({
                "library": STATE.get("root", ""),
                "done": done, "total": total, "running": running,
                "cache_bytes": csize,
                "version": "1.0",
                "appdir": app_dir(),
            })
            return

        if u.path == "/api/tracks":
            with STATE["lock"]:
                done, total, running = STATE["done"], STATE["total"], STATE["running"]
            out = []
            for t in STATE["tracks"]:
                out.append({
                    "i": t["i"], "name": t["name"], "artist": t["artist"],
                    "duration": round(t["duration"], 2), "segs": t["segs"],
                    "dominant": t["dominant"], "tempo": t["tempo"],
                    "key": t["key"], "counts": t["counts"],
                    "color": t["color"], "verdict": t["verdict"],
                })
            self._json({"tracks": out, "done": done, "total": total,
                        "running": running})
            return

        if u.path == "/cover":
            i = int(qs.get("i", ["0"])[0])
            t = self._track(i)
            if not t:
                self.send_error(404)
                return
            data, mime = cover_bytes(t)
            self._bytes(data, mime)
            return

        if u.path == "/audio":
            i = int(qs.get("i", ["0"])[0])
            t = self._track(i)
            if not t or not os.path.exists(t["path"]):
                self.send_error(404)
                return
            self._serve_audio(t["path"])
            return

        self.send_error(404)

    def _serve_audio(self, path: str):
        size = os.path.getsize(path)
        ext = os.path.splitext(path)[1].lower()
        ctype = MIME.get(ext, "application/octet-stream")
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        partial = False
        if rng and rng.startswith("bytes="):
            spec = rng[6:].split("-", 1)
            try:
                if spec[0]:
                    start = int(spec[0])
                if len(spec) > 1 and spec[1]:
                    end = int(spec[1])
                end = min(end, size - 1)
                if start > end:
                    start, end = 0, size - 1
                else:
                    partial = True
            except ValueError:
                start, end = 0, size - 1

        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        try:
            with open(path, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(65536, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass


# --------------------------------------------------------------------------- #
PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>音色谱</title>
<style>
*{box-sizing:border-box}
html,body{margin:0;height:100%;background:#08080b;color:#ededf4;
  font:14px/1.5 "Segoe UI","Microsoft YaHei",system-ui,sans-serif;overflow-x:hidden}
button{font-family:inherit;cursor:pointer;border:none;background:none;color:inherit}
::-webkit-scrollbar{width:9px;height:9px}
::-webkit-scrollbar-thumb{background:#2a2a34;border-radius:5px}

#bg{position:fixed;inset:0;z-index:0;width:100%;height:100%;
  opacity:.74;transition:opacity .5s;pointer-events:none}
body.paused #bg{opacity:.30}
.veil{position:fixed;inset:0;z-index:0;pointer-events:none;
  background:radial-gradient(ellipse at 50% 26%,rgba(8,8,11,.16),rgba(8,8,11,.80) 80%)}

.top{position:sticky;top:0;z-index:20;display:flex;align-items:center;gap:14px;
  padding:16px 28px;backdrop-filter:blur(18px);
  background:linear-gradient(to bottom,rgba(8,8,11,.88),rgba(8,8,11,.42));
  transition:opacity .28s}
.brand{font-size:16px;font-weight:600;letter-spacing:.04em;white-space:nowrap}
.brand span{color:#7ee0c0}
.search{flex:1;max-width:340px;position:relative}
.search input{width:100%;background:rgba(255,255,255,.07);border:1px solid rgba(255,255,255,.10);
  color:#ededf4;border-radius:20px;padding:8px 14px 8px 34px;font-size:13px;outline:none}
.search input:focus{background:rgba(255,255,255,.12);border-color:rgba(126,224,192,.5)}
.search::before{content:"⌕";position:absolute;left:13px;top:6px;color:#7a7a88;font-size:15px}
.chips{display:flex;gap:6px;flex-wrap:wrap}
.chip{font-size:12px;color:#9a9aa8;background:rgba(255,255,255,.05);
  border:1px solid rgba(255,255,255,.09);border-radius:14px;padding:4px 11px}
.chip:hover{color:#d8d8e4;background:rgba(255,255,255,.10)}
.chip.on{color:#0a0a0e;background:#7ee0c0;border-color:#7ee0c0;font-weight:600}
.tools{margin-left:auto;display:flex;gap:8px;align-items:center}
.tbtn{width:34px;height:34px;border-radius:50%;background:rgba(255,255,255,.07);
  border:1px solid rgba(255,255,255,.10);display:grid;place-items:center;font-size:14px}
.tbtn:hover{background:rgba(255,255,255,.16)}
.prog{font-size:11.5px;color:#7a7a88;white-space:nowrap}

main{position:relative;z-index:1;padding:22px 28px 150px;max-width:1500px;
  margin:0 auto;transition:opacity .28s}
.grid{display:grid;gap:26px 20px;
  grid-template-columns:repeat(auto-fill,minmax(160px,1fr))}
.item{cursor:pointer;transition:transform .16s}
.item:hover{transform:translateY(-4px)}
.cover{position:relative;aspect-ratio:1;border-radius:10px;overflow:hidden;
  box-shadow:0 6px 22px rgba(0,0,0,.45);background:#15151c}
.cover img{width:100%;height:100%;display:block;object-fit:cover}
.cover::after{content:'';position:absolute;left:0;right:0;bottom:0;height:34%;
  background:linear-gradient(to top,rgba(0,0,0,.30),transparent);pointer-events:none}
.cover .veil2{position:absolute;inset:0;background:rgba(0,0,0,.16);
  display:grid;place-items:center;transition:.16s}
.item:hover .veil2{background:rgba(0,0,0,.44)}
.cover .pb{width:42px;height:42px;border-radius:50%;background:rgba(255,255,255,.94);
  color:#0a0a0e;display:grid;place-items:center;font-size:15px;opacity:0;
  transform:scale(.86);transition:.16s;padding-left:2px}
.item:hover .pb{opacity:1;transform:scale(1)}
.item.active .cover{outline:2px solid #7ee0c0;outline-offset:2px}
.item.active .cover .pb{opacity:1;transform:scale(1);background:#7ee0c0}
.t{margin-top:9px;font-size:13.5px;font-weight:500;line-height:1.35;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.a{margin-top:2px;font-size:11.5px;color:#85858f;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.tag{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:5px;
  vertical-align:1px;background:#4a4a58}
.empty{color:#6a6a78;text-align:center;padding:60px 0;font-size:13.5px;
  grid-column:1/-1}

.drawer{position:fixed;top:0;right:0;bottom:0;width:330px;z-index:40;
  background:rgba(14,14,19,.94);backdrop-filter:blur(26px);
  border-left:1px solid rgba(255,255,255,.08);
  transform:translateX(100%);transition:transform .26s cubic-bezier(.3,.8,.3,1);
  display:flex;flex-direction:column}
.drawer.open{transform:none}
.dh{display:flex;align-items:center;gap:10px;padding:18px 18px 12px;
  border-bottom:1px solid rgba(255,255,255,.07)}
.dh b{font-size:14px;font-weight:600}
.dh span{color:#7a7a88;font-size:12px}
.dh button{margin-left:auto;font-size:18px;color:#8a8a98;line-height:1}
.dlist{flex:1;overflow-y:auto;padding:8px 10px 20px}
.drow{display:flex;align-items:center;gap:10px;padding:7px 8px;border-radius:8px;
  cursor:pointer}
.drow:hover{background:rgba(255,255,255,.07)}
.drow.on{background:rgba(126,224,192,.13)}
.drow img{width:36px;height:36px;border-radius:5px;flex:none;object-fit:cover;
  background:#1b1b24}
.drow .dt{flex:1;min-width:0}
.drow .dn{font-size:12.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.drow .da{font-size:11px;color:#7a7a88;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap}
.drow .dd{font-size:11px;color:#6a6a78;font-variant-numeric:tabular-nums}

.player{position:fixed;left:0;right:0;bottom:0;z-index:30;display:flex;
  align-items:center;gap:16px;padding:12px 24px;
  background:rgba(12,12,17,.88);backdrop-filter:blur(26px);
  border-top:1px solid rgba(255,255,255,.08);transition:opacity .28s}
.pc{width:52px;height:52px;border-radius:8px;object-fit:cover;flex:none;
  box-shadow:0 3px 14px rgba(0,0,0,.5);background:#1b1b24}
.pinfo{width:190px;flex:none;min-width:0;cursor:pointer}
.pinfo:hover .pt{color:#7ee0c0}
.pinfo .pt{font-size:13.5px;font-weight:500;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap}
.pinfo .pa{font-size:11.5px;color:#85858f;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap;margin-top:2px}
.pinfo .pq{font-size:11px;margin-top:3px}
.ctrl{display:flex;align-items:center;gap:10px;flex:none}
.ctrl button{width:34px;height:34px;border-radius:50%;display:grid;place-items:center;
  font-size:14px;color:#cacad6}
.ctrl button:hover{background:rgba(255,255,255,.1);color:#fff}
.ctrl .big{width:44px;height:44px;background:#ededf4;color:#0a0a0e;font-size:16px;
  padding-left:2px}
.ctrl .big:hover{background:#fff;transform:scale(1.05)}
.ctrl button:disabled{opacity:.3;cursor:default}
.seek{flex:1;display:flex;align-items:center;gap:11px;min-width:120px}
.seek .tm{font-size:11.5px;color:#85858f;font-variant-numeric:tabular-nums;
  width:38px;flex:none}
.seek .tm.r{text-align:right}
.bar{position:relative;flex:1;height:5px;border-radius:3px;
  background:rgba(255,255,255,.14);cursor:pointer}
.bar .fill{position:absolute;left:0;top:0;bottom:0;border-radius:3px;
  background:#7ee0c0;width:0}
.bar:hover .fill{background:#9df0d4}
.pmeta{width:210px;flex:none;text-align:right;font-size:11.5px;color:#7a7a88;
  font-variant-numeric:tabular-nums;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap}
.pmeta b{color:#c8c8d4;font-weight:500}
.vol{width:76px;height:4px;border-radius:2px;background:rgba(255,255,255,.14);
  cursor:pointer;position:relative;flex:none}
.vol .vf{position:absolute;left:0;top:0;bottom:0;border-radius:2px;
  background:#8a8a98;width:85%}

.viz{position:fixed;inset:0;z-index:60;display:none;background:transparent}
body.is-viz .viz{display:block}
body.is-viz .top,body.is-viz main,body.is-viz .player{visibility:hidden}
body.is-viz .veil{opacity:0}
body.is-viz #bg{opacity:1 !important}
.viz::before{content:'';position:absolute;inset:0;pointer-events:none;
  background:linear-gradient(to bottom,rgba(0,0,0,.44),transparent 22%,
    transparent 72%,rgba(0,0,0,.66))}
.vh{position:absolute;top:0;left:0;right:0;padding:26px 32px;display:flex;
  justify-content:space-between;align-items:flex-start;
  text-shadow:0 1px 18px rgba(0,0,0,.65)}
.vh h1{font-size:21px;margin:0 0 6px;font-weight:600}
.vsub{font-size:13px;opacity:.76;font-variant-numeric:tabular-nums}
.vhr{text-align:right;display:flex;flex-direction:column;align-items:flex-end;gap:4px}
.vq{font-size:12.5px;font-weight:600;letter-spacing:.08em;padding:3px 11px;
  border-radius:20px;background:rgba(0,0,0,.42);
  border:1px solid rgba(255,255,255,.14)}
.vro{font-size:12.5px;opacity:.8;font-variant-numeric:tabular-nums}
.vclose{margin-top:8px;width:34px;height:34px;border-radius:50%;
  background:rgba(0,0,0,.42);border:1px solid rgba(255,255,255,.16);
  color:#fff;font-size:19px;display:grid;place-items:center}
.vclose:hover{background:rgba(0,0,0,.66)}
.vbottom{position:absolute;left:0;right:0;bottom:0;padding:0 32px 26px}
#vtl{display:block;width:100%;height:46px;border-radius:7px;cursor:pointer;
  box-shadow:0 0 0 1px rgba(255,255,255,.14)}
.vctrl{display:flex;align-items:center;gap:14px;margin-top:14px}
.vctrl button{width:36px;height:36px;border-radius:50%;display:grid;
  place-items:center;font-size:14px;color:#dcdce6;background:rgba(255,255,255,.10);
  border:1px solid rgba(255,255,255,.18)}
.vctrl button:hover{background:rgba(255,255,255,.24)}
.vctrl .vbig{width:50px;height:50px;background:#ededf4;color:#0a0a0e;
  font-size:17px;padding-left:3px;border-color:transparent}
.vctrl .vbig:hover{background:#fff;transform:scale(1.04)}
#vtime{font-size:13px;opacity:.78;font-variant-numeric:tabular-nums}
.vhint{margin-left:auto;font-size:12px;opacity:.5}

/* ---------- 设置面板 ---------- */
.modal{position:fixed;inset:0;z-index:80;display:none;
  background:rgba(4,4,8,.62);backdrop-filter:blur(6px);place-items:center}
.modal.open{display:grid}
.mbox{width:min(92vw,470px);background:#14141b;border:1px solid #2a2a34;
  border-radius:14px;padding:20px 22px 18px;
  box-shadow:0 20px 60px rgba(0,0,0,.55)}
.mh{display:flex;align-items:center;margin-bottom:16px}
.mh b{font-size:15px;font-weight:600}
.mh button{margin-left:auto;font-size:19px;color:#8a8a98;line-height:1}
.mrow{display:flex;align-items:baseline;gap:12px;padding:7px 0;
  border-bottom:1px solid #1f1f28}
.mrow span{font-size:12.5px;color:#8a8a98;width:74px;flex:none}
.mrow b{font-size:12.5px;font-weight:500;color:#d8d8e4;word-break:break-all}
.mbtns{display:flex;gap:10px;margin:14px 0 6px}
.mbtn{flex:1;background:#22222c;border:1px solid #2f2f3a;color:#d8d8e4;
  border-radius:8px;padding:9px 0;font-size:13px}
.mbtn:hover{background:#2c2c38}
.mbtn.primary{background:#7ee0c0;border-color:#7ee0c0;color:#0a0a0e;font-weight:600}
.mbtn.primary:hover{background:#9df0d4}
.mfoot{margin-top:16px;font-size:11.5px;color:#5e5e70;text-align:center}

@media (max-width:760px){
  .top{padding:12px 14px;flex-wrap:wrap}
  .chips{order:3;width:100%}
  main{padding:16px 14px 150px}
  .grid{grid-template-columns:repeat(auto-fill,minmax(118px,1fr));gap:18px 12px}
  .player{flex-wrap:wrap;gap:10px;padding:10px 14px}
  .pinfo{width:calc(100% - 66px)}
  .seek{order:4;width:100%}
  .pmeta,.vol{display:none}
  .drawer{width:min(88vw,330px)}
  .vh{padding:16px 18px}
  .vh h1{font-size:17px}
  .vbottom{padding:0 18px 20px}
}
</style></head><body class="paused">

<canvas id="bg"></canvas>
<div class="veil"></div>

<div class="top">
  <div class="brand">音色<span>谱</span></div>
  <div class="search"><input id="q" placeholder="搜索歌名 / 艺术家"></div>
  <div class="chips">
    <button class="chip on" data-f="">全部</button>
    <button class="chip" data-f="明亮激昂">明亮激昂</button>
    <button class="chip" data-f="温暖平静">温暖平静</button>
    <button class="chip" data-f="紧张暗涌">紧张暗涌</button>
    <button class="chip" data-f="阴郁低沉">阴郁低沉</button>
  </div>
  <div class="tools">
    <span class="prog" id="prog"></span>
    <button class="tbtn" id="shuffle" title="随机播放">⤨</button>
    <button class="tbtn" id="openList" title="播放列表">☰</button>
    <button class="tbtn" id="openSet" title="设置">⚙</button>
  </div>
</div>

<main><div class="grid" id="grid"></div></main>

<aside class="drawer" id="drawer">
  <div class="dh"><b>播放列表</b><span id="dcount"></span>
    <button id="closeList">×</button></div>
  <div class="dlist" id="dlist"></div>
</aside>

<div class="modal" id="setModal">
  <div class="mbox">
    <div class="mh"><b>设置</b><button id="closeSet">×</button></div>
    <div class="mrow"><span>曲库</span><b id="setPath">—</b></div>
    <div class="mbtns">
      <button class="mbtn primary" id="btnChange">更换曲库</button>
      <button class="mbtn" id="btnRescan">重新扫描</button>
    </div>
    <div class="mrow"><span>曲目</span><b id="setCount">—</b></div>
    <div class="mrow"><span>分析进度</span><b id="setAnalyzed">—</b></div>
    <div class="mrow"><span>缓存</span><b id="setCache">—</b></div>
    <div class="mrow"><span>版本</span><b id="setVer">—</b></div>
    <div class="mfoot">关闭窗口即退出程序</div>
  </div>
</div>

<footer class="player">
  <img class="pc" id="pc" alt="">
  <div class="pinfo" id="pinfo">
    <div class="pt" id="pt">—</div>
    <div class="pa" id="pa"></div>
    <div class="pq" id="pq"></div>
  </div>
  <div class="ctrl">
    <button id="prev">⏮</button>
    <button class="big" id="play">▶</button>
    <button id="next">⏭</button>
  </div>
  <div class="seek">
    <span class="tm" id="ct">0:00</span>
    <div class="bar" id="bar"><div class="fill" id="fill"></div></div>
    <span class="tm r" id="dt">0:00</span>
  </div>
  <div class="vol" id="vol"><div class="vf" id="vf"></div></div>
  <div class="pmeta" id="pmeta"></div>
</footer>

<div class="viz" id="viz">
  <div class="vh">
    <div>
      <h1 id="vtitle">—</h1>
      <div class="vsub" id="vsub"></div>
    </div>
    <div class="vhr">
      <div class="vq" id="vq">—</div>
      <div class="vro" id="vro"></div>
      <button class="vclose" id="vclose" title="返回">×</button>
    </div>
  </div>
  <div class="vbottom">
    <canvas id="vtl"></canvas>
    <div class="vctrl">
      <button id="vprev">⏮</button>
      <button class="vbig" id="vplay">▶</button>
      <button id="vnext">⏭</button>
      <span id="vtime">0:00 / 0:00</span>
      <span class="vhint" id="vhint"></span>
    </div>
  </div>
</div>

<script>
// 出错时直接把信息显示在页面上，免得又变成"黑屏但不知道为啥"
window.onerror = function(m, s, l, c){
  try{
    var d = document.getElementById('__err');
    if(!d){
      d = document.createElement('div');
      d.id = '__err';
      d.style.cssText = 'position:fixed;left:0;right:0;bottom:0;z-index:99999;'
        + 'background:#7a1030;color:#fff;padding:10px 14px;'
        + 'font:12px/1.5 Consolas,monospace;white-space:pre-wrap;max-height:40vh;overflow:auto';
      document.body.appendChild(d);
    }
    d.textContent += (m + '  @' + l + ':' + c + '\n');
  }catch(e){}
  return false;
};
const QHEX = {'明亮激昂':'#e8a04f','温暖平静':'#5fd6a8',
              '紧张暗涌':'#d468a8','阴郁低沉':'#7b8fd0'};
let TRACKS = [];
let cur = -1, W = 0, H = 0, dpr = 1;

const audio = new Audio();
audio.preload = 'metadata';
const bg = document.getElementById('bg'), bctx = bg.getContext('2d');
const gridEl = document.getElementById('grid');
const dlistEl = document.getElementById('dlist');
const drawer = document.getElementById('drawer');
const pcEl = document.getElementById('pc');
const ptEl = document.getElementById('pt');
const paEl = document.getElementById('pa');
const pqEl = document.getElementById('pq');
const playBtn = document.getElementById('play');
const fillEl = document.getElementById('fill');
const ctEl = document.getElementById('ct');
const dtEl = document.getElementById('dt');
const pmetaEl = document.getElementById('pmeta');
const barEl = document.getElementById('bar');
const progEl = document.getElementById('prog');
const vtl = document.getElementById('vtl'), vctx = vtl.getContext('2d');
const vtitle = document.getElementById('vtitle'), vsub = document.getElementById('vsub');
const vq = document.getElementById('vq'), vro = document.getElementById('vro');
const vtime = document.getElementById('vtime');
const vhint = document.getElementById('vhint');

let actx = null, analyser = null;
const specBuf = new Uint8Array(512);

/* ---------- 色彩 ---------- */
function oklchToSrgb(L,C,hDeg){
  const h=hDeg*Math.PI/180,a=C*Math.cos(h),b=C*Math.sin(h);
  const l_=L+0.3963377774*a+0.2158037573*b;
  const m_=L-0.1055613458*a-0.0638541728*b;
  const s_=L-0.0894841775*a-1.2914855480*b;
  const l3=l_*l_*l_,m3=m_*m_*m_,s3=s_*s_*s_;
  const r=4.0767416621*l3-3.3077115913*m3+0.2309699292*s3;
  const g=-1.2684380046*l3+2.6097574011*m3-0.3413193965*s3;
  const bb=-0.0041960863*l3-0.7034186147*m3+1.7076147010*s3;
  const enc=x=>{x=Math.min(Math.max(x,0),1);
    return x<=0.0031308?x*12.92:1.055*Math.pow(x,1/2.4)-0.055;};
  return [enc(r)*255,enc(g)*255,enc(bb)*255];
}
const HA=[[45,35],[135,320],[225,255],[315,165]];
function hueFor(th){
  const t=((th%360)+360)%360;
  for(let i=0;i<4;i++){
    const [a0,h0]=HA[i],[a1,h1]=HA[(i+1)%4];
    let sp=a1-a0; if(sp<=0) sp+=360;
    const d=((t-a0)%360+360)%360;
    if(d<=sp) return (h0+(h1-h0)*(d/sp))%360;
  }
  return 35;
}
function vaRGB(v,a,Lb=0,Cb=0,hb=0){
  const th=Math.atan2(a-0.5,v-0.5)*180/Math.PI;
  const rad=Math.min(Math.hypot(v-0.5,a-0.5)/0.7071,1);
  const h=(hueFor(th)+hb+360)%360;
  return oklchToSrgb(0.26+0.48*v+Lb,
    Math.max(0.055+0.24*Math.pow(rad,0.75)+Cb,0), h);
}
function vaHex(v,a){
  const [r,g,b]=vaRGB(v,a,0.20,-0.006);
  return '#'+[r,g,b].map(x=>Math.round(x).toString(16).padStart(2,'0')).join('');
}
function quadOf(v,a){
  if(v>=.5&&a>=.5) return '明亮激昂';
  if(v>=.5) return '温暖平静';
  if(a>=.5) return '紧张暗涌';
  return '阴郁低沉';
}
function mmss(t){ return `${Math.floor(t/60)}:${String(Math.floor(t%60)).padStart(2,'0')}`; }

/* ---------- 数据 ---------- */
function segAt(tr,t){
  const s=tr.segs;
  if(!s||!s.length) return {v:.5,a:.5,e:.3};
  const n=s.length/3, step=tr.duration? tr.duration/n : 4;
  if(t<=0) return {v:s[0],a:s[1],e:s[2]};
  const fi=t/step;
  if(fi>=n-1) return {v:s[(n-1)*3],a:s[(n-1)*3+1],e:s[(n-1)*3+2]};
  const i=Math.floor(fi), p=fi-i;
  const j=i*3, k=(i+1)*3;
  return {v:s[j]+(s[k]-s[j])*p, a:s[j+1]+(s[k+1]-s[j+1])*p, e:s[j+2]+(s[k+2]-s[j+2])*p};
}

/* ---------- 音频图 ---------- */
function initGraph(){
  if(actx){ if(actx.state==='suspended') actx.resume(); return; }
  try{
    actx=new (window.AudioContext||window.webkitAudioContext)();
    const src=actx.createMediaElementSource(audio);
    analyser=actx.createAnalyser();
    analyser.fftSize=1024; analyser.smoothingTimeConstant=0.78;
    src.connect(analyser); analyser.connect(actx.destination);
  }catch(e){actx=null;analyser=null;}
}

/* ---------- 背景 ---------- */
let blobs=[];
for(let i=0;i<6;i++) blobs.push({
  x:Math.random(),y:Math.random(),vx:(Math.random()-0.5)*0.00013,
  vy:(Math.random()-0.5)*0.00013,r:0.30+Math.random()*0.30,
  ph:Math.random()*6.283,sp:6+Math.random()*10,hoff:(Math.random()-0.5)*280});

function resize(){
  dpr=Math.min(window.devicePixelRatio||1,2);
  W=window.innerWidth;H=window.innerHeight;
  bg.width=W*dpr;bg.height=H*dpr;bctx.setTransform(dpr,0,0,dpr,0,0);
  const vw=vtl.clientWidth||W-64;
  vtl.width=vw*dpr;vtl.height=46*dpr;vctx.setTransform(dpr,0,0,dpr,0,0);
  drawVtl();
  try{ draw(performance.now()); }catch(e){}
}
window.addEventListener('resize',resize);

let tp=performance.now();
function draw(now){
  const dt=Math.min(now-tp,60);tp=now;
  const vizOn=document.body.classList.contains('is-viz');
  const t=(cur>=0?audio.currentTime:0);
  const p=(cur>=0?segAt(TRACKS[cur],t):{v:.5,a:.5,e:.3});
  const tempo=(cur>=0&&TRACKS[cur].tempo)?TRACKS[cur].tempo:100;
  const beat=60000/Math.max(tempo,30);

  bctx.globalCompositeOperation='source-over';
  const c0=vaRGB(p.v,p.a,-0.14,-0.06);
  bctx.fillStyle=`rgb(${c0[0]|0},${c0[1]|0},${c0[2]|0})`;
  bctx.fillRect(0,0,W,H);

  const pulse=1+0.26*p.e*Math.sin(now/beat*Math.PI);
  for(const b of blobs){
    b.x+=b.vx*dt*(0.5+p.a);b.y+=b.vy*dt*(0.5+p.a);
    if(b.x<-0.45)b.x=1.45; if(b.x>1.45)b.x=-0.45;
    if(b.y<-0.45)b.y=1.45; if(b.y>1.45)b.y=-0.45;
    const cx=b.x*W,cy=b.y*H;
    const rr=b.r*Math.min(W,H)*(0.60+0.70*p.a)*pulse;
    const gr=bctx.createRadialGradient(cx,cy,0,cx,cy,rr);
    const hue=b.hoff+28*Math.sin(now*0.00042*b.sp+b.ph);
    const c1=vaRGB(p.v,p.a,0.075+0.055*p.e,0.075,hue);
    const c2=vaRGB(p.v,p.a,0.020,0.035,hue);
    gr.addColorStop(0,`rgba(${c1[0]|0},${c1[1]|0},${c1[2]|0},1)`);
    gr.addColorStop(0.55,`rgba(${c2[0]|0},${c2[1]|0},${c2[2]|0},1)`);
    gr.addColorStop(1,'rgba(0,0,0,0)');
    bctx.globalAlpha=0.40+0.20*p.e;
    bctx.fillStyle=gr;
    bctx.beginPath();bctx.arc(cx,cy,rr,0,Math.PI*2);bctx.fill();
  }
  bctx.globalAlpha=1;

  if(analyser&&!audio.paused){
    analyser.getByteFrequencyData(specBuf);
    const N=56, baseY=vizOn?H*0.74:H*0.94;
    const maxH=vizOn?H*0.42:H*0.24, bw=W*0.86/N, x0=W*0.07;
    bctx.globalAlpha=vizOn?0.78:0.42;
    for(let i=0;i<N;i++){
      const k=Math.floor(Math.pow(i/N,1.55)*(specBuf.length*0.62));
      const amp=(specBuf[k]||0)/255;
      const h=Math.max(amp*maxH*(0.55+0.75*p.e),1);
      const c=vaRGB(p.v,p.a,0.02+0.22*amp,0.02,(i/N-0.5)*46);
      bctx.fillStyle=`rgb(${c[0]|0},${c[1]|0},${c[2]|0})`;
      bctx.fillRect(x0+i*bw+bw*0.16,baseY-h,bw*0.68,h);
    }
    bctx.globalAlpha=1;
  }

  if(vizOn){
    const q=quadOf(p.v,p.a);
    vq.textContent=q; vq.style.color=QHEX[q];
    vro.textContent=`V ${p.v.toFixed(2)}  A ${p.a.toFixed(2)}`;
  }
}
function frame(now){ draw(now); requestAnimationFrame(frame); }

/* ---------- 时间轴 ---------- */
function drawVtl(){
  const w=vtl.clientWidth||1,h=46;
  vctx.clearRect(0,0,w,h);
  if(cur<0||!TRACKS[cur]||!TRACKS[cur].segs){ vctx.fillStyle='#1a1a22'; vctx.fillRect(0,0,w,h); return; }
  const tr=TRACKS[cur], s=tr.segs, n=s.length/3;
  for(let i=0;i<n;i++){
    const x0=i/n*w, x1=(i+1)/n*w;
    const [r,g,b]=vaRGB(s[i*3],s[i*3+1],0.20,-0.006);
    vctx.fillStyle=`rgb(${r|0},${g|0},${b|0})`;
    vctx.fillRect(x0,0,Math.max(x1-x0,0.6),h);
  }
  const px=(audio.currentTime/(tr.duration||1))*w;
  vctx.fillStyle='rgba(0,0,0,.30)';vctx.fillRect(0,0,px,h);
  vctx.fillStyle='rgba(255,255,255,.95)';vctx.fillRect(px-1,0,2.5,h);
}
vtl.addEventListener('click',e=>{
  if(cur<0) return;
  const r=vtl.getBoundingClientRect();
  audio.currentTime=Math.min(Math.max((e.clientX-r.left)/r.width,0),1)*TRACKS[cur].duration;
  drawVtl();
});

/* ---------- 建界面 ---------- */
function coverURL(i){ return '/cover?i='+i; }
function audioURL(i){ return '/audio?i='+i; }

function buildGrid(){
  gridEl.innerHTML='';
  TRACKS.forEach((tr,i)=>{
    const q=tr.dominant||'';
    const el=document.createElement('div');
    el.className='item';
    el.dataset.q=q;
    el.dataset.n=(tr.name+' '+(tr.artist||'')).toLowerCase();
    el.dataset.i=i;
    el.innerHTML=`
      <div class="cover">
        <img src="${coverURL(i)}" loading="${i<40?'eager':'lazy'}" decoding="async" alt="">
        <div class="veil2"><div class="pb">▶</div></div>
      </div>
      <div class="t" title="${escapeHtml(tr.name)}">${escapeHtml(tr.name)}</div>
      <div class="a"><span class="tag" style="background:${q?QHEX[q]:'#4a4a58'}"></span>${escapeHtml(tr.artist||'未知艺术家')}</div>`;
    el.addEventListener('click',()=>{ select(i); openViz(); });
    gridEl.appendChild(el);
  });
}

function escapeHtml(s){
  return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function buildDrawer(){
  dlistEl.innerHTML='';
  TRACKS.forEach((tr,i)=>{
    const el=document.createElement('div');
    el.className='drow'; el.dataset.i=i;
    el.innerHTML=`<img src="${coverURL(i)}" loading="lazy" alt="">
      <div class="dt"><div class="dn">${escapeHtml(tr.name)}</div>
      <div class="da">${escapeHtml(tr.artist||'—')}</div></div>
      <div class="dd">${mmss(tr.duration||0)}</div>`;
    el.addEventListener('click',()=>select(i));
    dlistEl.appendChild(el);
  });
  document.getElementById('dcount').textContent=TRACKS.length+' 首';
}

let fq='',ft='';
function applyFilter(){
  let n=0;
  gridEl.querySelectorAll('.item').forEach(el=>{
    const ok=(!fq||el.dataset.q===fq)&&(!ft||el.dataset.n.includes(ft));
    el.classList.toggle('hide',!ok); if(ok) n++;
  });
  let e=gridEl.querySelector('.empty');
  if(!n&&!e){ e=document.createElement('div'); e.className='empty';
    e.textContent='没有匹配的曲目'; gridEl.appendChild(e); }
  else if(n&&e) e.remove();
  progEl.textContent=n+' / '+TRACKS.length;
}
document.getElementById('q').addEventListener('input',e=>{
  ft=e.target.value.trim().toLowerCase(); applyFilter();
});
document.querySelectorAll('.chip').forEach(b=>{
  b.addEventListener('click',()=>{
    document.querySelectorAll('.chip').forEach(x=>x.classList.remove('on'));
    b.classList.add('on'); fq=b.dataset.f; applyFilter();
  });
});

/* ---------- 播放控制 ---------- */
function select(i,autoplay){
  const tr=TRACKS[i]; if(!tr) return;
  cur=i;
  pcEl.src=coverURL(i);
  ptEl.textContent=tr.name;
  paEl.textContent=tr.artist||'';
  pqEl.innerHTML=tr.dominant
    ? `<span style="color:${QHEX[tr.dominant]}">● ${tr.dominant}</span> · ${(tr.tempo||0).toFixed(0)} BPM · ${tr.key||'—'}`
    : `<span style="opacity:.5">分析中…</span>`;
  pmetaEl.innerHTML=tr.dominant?`<b>${tr.dominant}</b> · ${(tr.tempo||0).toFixed(0)} BPM`:'';
  pmetaEl.title=tr.verdict||'';
  dtEl.textContent=mmss(tr.duration||0);
  ctEl.textContent='0:00'; fillEl.style.width='0%';
  document.title=tr.name+' — 音色谱';

  vtitle.textContent=tr.name;
  vsub.textContent=`${mmss(tr.duration||0)} · ${(tr.tempo||0).toFixed(0)} BPM · ${tr.key||'—'}`;
  vtime.textContent='0:00 / '+mmss(tr.duration||0);
  vhint.textContent=tr.dominant?('主导「'+tr.dominant+'」'):'分析中…';

  gridEl.querySelectorAll('.item').forEach((el,j)=>el.classList.toggle('active',j===i));
  dlistEl.querySelectorAll('.drow').forEach((el,j)=>el.classList.toggle('on',j===i));

  audio.src=audioURL(i);
  playBtn.textContent='▶';
  document.body.classList.add('paused');
  drawVtl();
  if(autoplay!==false) doPlay();
}

function doPlay(){
  if(cur<0) return;
  initGraph();
  audio.play().then(()=>{
    playBtn.textContent='❚❚';
    document.querySelector('#vplay').textContent='❚❚';
    document.body.classList.remove('paused');
  }).catch(()=>{});
}
function doPause(){
  audio.pause();
  playBtn.textContent='▶';
  document.querySelector('#vplay').textContent='▶';
  document.body.classList.add('paused');
}

playBtn.addEventListener('click',()=>audio.paused?doPlay():doPause());
document.querySelector('#vplay').addEventListener('click',()=>audio.paused?doPlay():doPause());

function step(d){
  const n=TRACKS.length;
  if(!n) return;
  select(((cur+d)%n+n)%n);
}
document.getElementById('next').addEventListener('click',()=>step(1));
document.getElementById('vnext').addEventListener('click',()=>step(1));
document.getElementById('prev').addEventListener('click',()=>{
  if(audio.currentTime>3){audio.currentTime=0;return;} step(-1);
});
document.getElementById('vprev').addEventListener('click',()=>{
  if(audio.currentTime>3){audio.currentTime=0;return;} step(-1);
});
document.getElementById('shuffle').addEventListener('click',()=>{
  if(!TRACKS.length) return;
  select(Math.floor(Math.random()*TRACKS.length));
  openViz();
});

audio.addEventListener('timeupdate',()=>{
  if(cur<0) return;
  const tr=TRACKS[cur], d=tr.duration||1;
  ctEl.textContent=mmss(audio.currentTime);
  vtime.textContent=mmss(audio.currentTime)+' / '+mmss(d);
  fillEl.style.width=(audio.currentTime/d*100)+'%';
  drawVtl();
});
audio.addEventListener('ended',()=>step(1));
audio.addEventListener('pause',()=>{
  playBtn.textContent='▶';
  document.querySelector('#vplay').textContent='▶';
  document.body.classList.add('paused');
});
audio.addEventListener('play',()=>{
  playBtn.textContent='❚❚';
  document.querySelector('#vplay').textContent='❚❚';
  document.body.classList.remove('paused');
});
barEl.addEventListener('click',e=>{
  if(cur<0) return;
  const r=barEl.getBoundingClientRect();
  audio.currentTime=Math.min(Math.max((e.clientX-r.left)/r.width,0),1)*(TRACKS[cur].duration||1);
});

/* ---------- 可视化开关 ---------- */
function openViz(){
  if(cur<0) return;
  try{
    document.querySelector('.top').style.display='none';
    document.querySelector('main').style.display='none';
    document.querySelector('.player').style.display='none';
    const vl=document.querySelector('.veil');
    if(vl) vl.style.display='none';
    const v=document.getElementById('viz');
    v.style.display='block';
    v.style.opacity='1';
    v.style.pointerEvents='auto';
    v.style.background='transparent';
    const b=document.getElementById('bg');
    b.style.opacity='1';
    b.style.zIndex='0';
  }catch(e){ /* 出错不拦路，下面照样渲染 */ }
  document.body.classList.add('is-viz');
  // 先把当前帧画出来，避免切换到可视化瞬间是空白
  try{ draw(performance.now()); }catch(e){}
  setTimeout(()=>{
    // 可视化层刚显示时布局还没算完，时间轴 canvas 尺寸会是 0，这里重算一次
    try{ resize(); }catch(e){}
    drawVtl();
    try{ draw(performance.now()); }catch(e){}
    if(location.search.indexOf('diag')>=0){
      try{
        const d=bctx.getImageData(Math.floor(bg.width/2), Math.floor(bg.height/2), 1, 1).data;
        const cs=getComputedStyle(document.getElementById('viz'));
        const bcs=getComputedStyle(bg);
        fetch('/report',{method:'POST',body:JSON.stringify({
          w:W, h:H, cw:bg.width, ch:bg.height,
          center:[d[0],d[1],d[2],d[3]],
          vizDisplay:cs.display, vizBg:cs.backgroundColor, vizZ:cs.zIndex,
          bgOpacity:bcs.opacity, bgZ:bcs.zIndex, bgDisplay:bcs.display,
          bodyClass:document.body.className,
          vhVisible:getComputedStyle(document.querySelector('.vh')).visibility,
          vtlSize:[vtl.clientWidth, vtl.clientHeight],
          cur, segs: (TRACKS[cur]&&TRACKS[cur].segs)?TRACKS[cur].segs.length:0,
        })});
      }catch(e){}
    }
    if(location.search.indexOf('shot')>=0){
      try{
        const url=bg.toDataURL('image/png');
        fetch('/shot',{method:'POST',body:url});
        const im=document.createElement('img');
        im.src=url;
        im.style.cssText='position:fixed;inset:0;width:100%;height:100%;'
          +'object-fit:cover;z-index:1;pointer-events:none';
        document.body.appendChild(im);
      }catch(e){}
    }
  },120);
}
function closeViz(){
  document.body.classList.remove('is-viz');
  document.querySelector('.top').style.display='';
  document.querySelector('main').style.display='';
  document.querySelector('.player').style.display='';
  document.querySelector('.veil').style.display='';
  const v=document.getElementById('viz');
  v.style.display='none';
  v.style.background='';
  document.getElementById('bg').style.opacity='';
}
document.getElementById('vclose').addEventListener('click',closeViz);
document.getElementById('pinfo').addEventListener('click',openViz);
document.getElementById('openList').addEventListener('click',()=>drawer.classList.add('open'));
document.getElementById('closeList').addEventListener('click',()=>drawer.classList.remove('open'));
document.addEventListener('keydown',e=>{
  if(e.target.tagName==='INPUT') return;
  if(e.key==='Escape'){ drawer.classList.remove('open'); closeViz(); }
  if(e.key===' '){ e.preventDefault(); audio.paused?doPlay():doPause(); }
  if(e.key==='ArrowRight') step(1);
  if(e.key==='ArrowLeft') step(-1);
});

/* ---------- 设置面板 ---------- */
const setModal = document.getElementById('setModal');
const btnChange = document.getElementById('btnChange');
const btnRescan = document.getElementById('btnRescan');

function fmtBytes(n){
  if(!n) return '0 KB';
  if(n > 1048576) return (n/1048576).toFixed(1) + ' MB';
  return Math.round(n/1024) + ' KB';
}

async function openSettings(){
  setModal.classList.add('open');
  try{
    const s = await (await fetch('/api/status',{cache:'no-store'})).json();
    document.getElementById('setPath').textContent = s.library || '未设置';
    document.getElementById('setCount').textContent = s.total + ' 首';
    document.getElementById('setAnalyzed').textContent =
      s.running ? `${s.done} / ${s.total}（进行中）` : `${s.done} / ${s.total}（完成）`;
    document.getElementById('setCache').textContent = fmtBytes(s.cache_bytes);
    document.getElementById('setVer').textContent = 'v' + s.version;
  }catch(e){
    document.getElementById('setPath').textContent = '读取失败';
  }
}

document.getElementById('openSet').addEventListener('click', openSettings);
document.getElementById('closeSet').addEventListener('click', ()=>setModal.classList.remove('open'));
setModal.addEventListener('click', e=>{ if(e.target === setModal) setModal.classList.remove('open'); });

btnChange.addEventListener('click', async ()=>{
  btnChange.disabled = true;
  btnChange.textContent = '等待选择…';
  try{
    const r = await (await fetch('/api/set-library',{method:'POST'})).json();
    if(r.ok){
      lastSig = '';
      TRACKS = [];
      cur = -1;
      await pull();
      setTimeout(openSettings, 300);
    }
  }catch(e){}
  btnChange.disabled = false;
  btnChange.textContent = '更换曲库';
});

btnRescan.addEventListener('click', async ()=>{
  btnRescan.disabled = true;
  btnRescan.textContent = '扫描中…';
  try{
    await fetch('/api/rescan',{method:'POST'});
    lastSig = '';
    await pull();
    setTimeout(openSettings, 600);
  }catch(e){}
  btnRescan.disabled = false;
  btnRescan.textContent = '重新扫描';
});

/* ---------- 音量 ---------- */
const volEl=document.getElementById('vol'),vfEl=document.getElementById('vf');
audio.volume=0.85;
volEl.addEventListener('click',e=>{
  const r=volEl.getBoundingClientRect();
  audio.volume=Math.min(Math.max((e.clientX-r.left)/r.width,0),1);
  vfEl.style.width=(audio.volume*100)+'%';
});

/* ---------- 拉数据 ---------- */
const WANT_VIZ = location.hash === '#viz'
  || new URLSearchParams(location.search).has('viz')
  || document.body.classList.contains('is-viz');
let lastSig='';
async function pull(){
  try{
    const r=await fetch('/api/tracks',{cache:'no-store'});
    const d=await r.json();
    const sig=d.tracks.map(t=>t.segs?1:0).join('')+'|'+d.done;
    if(sig!==lastSig){
      const first=TRACKS.length===0;
      TRACKS=d.tracks;
      lastSig=sig;
      if(first){
        buildGrid(); buildDrawer();
        if(WANT_VIZ && TRACKS.length){ select(0,false); openViz(); }
      }
      else refreshMeta();
      applyFilter();
      if(d.running) progEl.textContent=`分析中 ${d.done}/${d.total}`;
    }
    setTimeout(pull, d.running?2500:15000);
  }catch(e){ setTimeout(pull,4000); }
}

function refreshMeta(){
  gridEl.querySelectorAll('.item').forEach((el,i)=>{
    const tr=TRACKS[i]; if(!tr) return;
    el.dataset.q=tr.dominant||'';
    const tag=el.querySelector('.tag');
    if(tag&&tr.dominant) tag.style.background=QHEX[tr.dominant];
  });
  if(cur>=0){
    const tr=TRACKS[cur];
    pqEl.innerHTML=tr.dominant
      ? `<span style="color:${QHEX[tr.dominant]}">● ${tr.dominant}</span> · ${(tr.tempo||0).toFixed(0)} BPM · ${tr.key||'—'}`
      : `<span style="opacity:.5">分析中…</span>`;
    pmetaEl.innerHTML=tr.dominant?`<b>${tr.dominant}</b> · ${(tr.tempo||0).toFixed(0)} BPM`:'';
    pmetaEl.title=tr.verdict||'';
    vhint.textContent=tr.dominant?('主导「'+tr.dominant+'」'):'分析中…';
    drawVtl();
  }
}

/* ---------- 启动 ---------- */
resize();
pull();
requestAnimationFrame(frame);
</script>
</body></html>"""


# --------------------------------------------------------------------------- #
def pick_port(preferred: int = 8765) -> int:
    for p in range(preferred, preferred + 40):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
    return 0


def ask_folder() -> str:
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox
        r = tk.Tk()
        r.withdraw()
        messagebox.showinfo("音色谱", "第一次运行，请选择你的音乐文件夹。\n\n"
                                      "之后会自动记住，也可在面板里更换。")
        d = filedialog.askdirectory(title="选择音乐文件夹")
        r.destroy()
        return d or ""
    except Exception:
        return ""


def resolve_library(cli_root: str) -> str:
    if cli_root and os.path.isdir(cli_root):
        return cli_root
    cfg = load_config()
    lib = cfg.get("library", "")
    if lib and os.path.isdir(lib):
        return lib
    d = ask_folder()
    if d:
        cfg["library"] = d
        save_config(cfg)
    return d


WIN = {"w": None}


def start_scan_analyze(root: str) -> int:
    tracks = scan(root)
    if not tracks:
        return 0
    STATE["tracks"] = tracks
    STATE["root"] = root
    cache_path = os.path.join(app_dir(), "cache.json")
    with STATE["lock"]:
        STATE["total"] = len(tracks)
        STATE["done"] = 0
        STATE["running"] = True
    threading.Thread(target=analyze_all, args=(root, cache_path), daemon=True).start()
    return len(tracks)


def control_panel(url: str, root: str) -> None:
    """一个小的控制面板：显示进度、打开播放器、换曲库、退出。"""
    import tkinter as tk
    from tkinter import messagebox

    rootw = tk.Tk()
    rootw.title("音色谱")
    rootw.geometry("480x248")
    rootw.resizable(False, False)
    rootw.configure(bg="#101014")
    try:
        rootw.iconbitmap(default="")
    except Exception:
        pass

    def lbl(txt, size=10, fg="#d8d8e4", bg="#101014"):
        return tk.Label(rootw, text=txt, font=("Microsoft YaHei UI", size),
                        fg=fg, bg=bg, anchor="w", justify="left")

    lbl("音色谱", 16, "#ffffff").place(x=22, y=16)
    lbl("音乐情绪色彩播放器", 9, "#7a7a88").place(x=24, y=48)

    path_var = tk.StringVar(value=root)
    lbl_var = tk.Label(rootw, textvariable=path_var, font=("Microsoft YaHei UI", 8),
                       fg="#8a8a98", bg="#101014", anchor="w", wraplength=430)
    lbl_var.place(x=24, y=74)

    status_var = tk.StringVar(value="准备中…")
    tk.Label(rootw, textvariable=status_var, font=("Microsoft YaHei UI", 10),
             fg="#7ee0c0", bg="#101014", anchor="w").place(x=24, y=112)

    bar_bg = tk.Frame(rootw, bg="#22222c", width=432, height=5)
    bar_bg.place(x=24, y=142)
    bar_fg = tk.Frame(rootw, bg="#7ee0c0", width=0, height=5)
    bar_fg.place(x=24, y=142)

    def open_player():
        open_app_window(url)

    def change_lib():
        d = ask_folder()
        if not d or d == root:
            return
        cfg = load_config()
        cfg["library"] = d
        save_config(cfg)
        n = start_scan_analyze(d)
        if n == 0:
            messagebox.showwarning("音色谱", "这个文件夹里没有找到音频文件。")
            return
        path_var.set(d)

    def do_quit():
        rootw.destroy()

    tk.Button(rootw, text="打开播放器", command=open_player,
              font=("Microsoft YaHei UI", 10), bg="#7ee0c0", fg="#0a0a0e",
              relief="flat", cursor="hand2", width=12).place(x=24, y=176)
    tk.Button(rootw, text="更换曲库", command=change_lib,
              font=("Microsoft YaHei UI", 10), bg="#25252f", fg="#d8d8e4",
              relief="flat", cursor="hand2", width=12).place(x=152, y=176)
    tk.Button(rootw, text="退出", command=do_quit,
              font=("Microsoft YaHei UI", 10), bg="#25252f", fg="#d8d8e4",
              relief="flat", cursor="hand2", width=8).place(x=280, y=176)

    def tick():
        with STATE["lock"]:
            done, total, running = STATE["done"], STATE["total"], STATE["running"]
        if running:
            status_var.set(f"正在分析情绪  {done} / {total}")
            w = int(432 * (done / max(total, 1)))
            bar_fg.configure(width=max(w, 2))
        else:
            status_var.set(f"就绪 · {total} 首 · 服务运行中")
            bar_fg.configure(width=432)
        rootw.after(600, tick)

    tick()
    rootw.mainloop()


def main() -> None:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    args = [a for a in sys.argv[1:]]
    port = None
    cli_root = None
    no_gui = False
    i = 0
    while i < len(args):
        if args[i] == "--port" and i + 1 < len(args):
            port = int(args[i + 1]); i += 2; continue
        if args[i] == "--no-gui":
            no_gui = True; i += 1; continue
        if not args[i].startswith("--"):
            cli_root = args[i]
        i += 1

    root = resolve_library(cli_root)
    if not root:
        print("没有选择曲库，退出。")
        return

    print(f"扫描曲库：{root}")
    t0 = time.time()
    n = start_scan_analyze(root)
    if n == 0:
        print("这个目录里没找到音频文件（mp3 / flac / m4a / wav / ogg）。")
        return
    print(f"  {n} 首，用时 {time.time()-t0:.1f}s")
    print("后台分析情绪（结果会缓存，下次秒开）…")

    port = port or pick_port()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}/"
    print(f"\n服务已启动：{url}\n")

    if no_gui:
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    else:
        # 单窗口：WebView2 宿主，无地址栏、无 Edge 媒体工具栏，关窗即退出
        import webview
        win = webview.create_window(
            "音色谱", url,
            width=1320, height=860,
            min_size=(900, 600),
            background_color="#08080b",
            text_select=False,
        )
        WIN["w"] = win
        webview.start()

    srv.shutdown()
    srv.server_close()


if __name__ == "__main__":
    main()
