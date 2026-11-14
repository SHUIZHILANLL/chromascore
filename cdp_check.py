#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
cdp_check.py — 用 CDP 驱动真实浏览器做验证
==========================================
`msedge --headless --screenshot` 不合成 canvas，本项目的色场全在 canvas 上，
所以必须走 CDP 才能拿到真实画面。

    python cdp_check.py <url> <out.png> [--click SELECTOR] [--eval JS] [--wait MS]

输出：截图 + 一段页面状态 JSON
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

import websockets

EDGE = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]
PORT = 9345


def find_edge() -> str:
    for p in EDGE:
        if os.path.exists(p):
            return p
    raise SystemExit("Edge not found")


async def call(ws, mid, method, params=None):
    await ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
    while True:
        m = json.loads(await ws.recv())
        if m.get("id") == mid:
            if "error" in m:
                raise RuntimeError(f"{method}: {m['error']}")
            return m.get("result", {})


STATE_JS = """(() => {
  const bg = document.getElementById('bg');
  const viz = document.getElementById('viz');
  const sm = document.getElementById('setModal');
  let px = null;
  if (bg) {
    try {
      const d = bg.getContext('2d').getImageData(
        Math.floor(bg.width/2), Math.floor(bg.height/2), 1, 1).data;
      px = [d[0], d[1], d[2], d[3]];
    } catch(e) { px = 'ERR'; }
  }
  const r = bg ? bg.getBoundingClientRect() : null;
  return JSON.stringify({
    title: document.title,
    bodyClass: document.body.className,
    bodyDisplay: getComputedStyle(document.body).display,
    bodySize: [document.body.offsetWidth, document.body.offsetHeight],
    bgRect: r ? [r.x|0, r.y|0, r.width|0, r.height|0] : null,
    bgCenter: px,
    vizDisplay: viz ? getComputedStyle(viz).display : null,
    setOpen: sm ? sm.classList.contains('open') : null,
    setPath: (document.getElementById('setPath')||{}).textContent || null,
    setCount: (document.getElementById('setCount')||{}).textContent || null,
    items: document.querySelectorAll('.item').length,
    errText: (document.getElementById('__err')||{}).textContent || null,
  });
})()"""


async def run(url, out, click=None, evals=None, wait=9000):
    edge = find_edge()
    profile = os.path.join(tempfile.gettempdir(), f"csc_{int(time.time())}")
    proc = subprocess.Popen([
        edge, f"--remote-debugging-port={PORT}", f"--user-data-dir={profile}",
        "--no-first-run", "--no-default-browser-check",
        "--window-position=-32000,-32000", "--window-size=1400,900", "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    try:
        ws_url = None
        for _ in range(40):
            time.sleep(0.5)
            try:
                tabs = json.load(urllib.request.urlopen(
                    f"http://127.0.0.1:{PORT}/json", timeout=2))
                pages = [t for t in tabs if t.get("type") == "page"
                         and t.get("webSocketDebuggerUrl")]
                if pages:
                    ws_url = pages[0]["webSocketDebuggerUrl"]
                    break
            except Exception:
                pass
        if not ws_url:
            raise RuntimeError("CDP not ready")

        async with websockets.connect(ws_url, max_size=80 * 1024 * 1024) as ws:
            await call(ws, 1, "Page.enable")
            await call(ws, 2, "Runtime.enable")
            await call(ws, 3, "Page.navigate", {"url": url})
            await asyncio.sleep(wait / 1000.0)

            if click:
                await call(ws, 4, "Runtime.evaluate",
                           {"expression": f"document.querySelector('{click}').click()"})
                await asyncio.sleep(1.5)

            for m in (evals or []):
                await call(ws, 5, "Runtime.evaluate", {"expression": m})
                await asyncio.sleep(1.2)

            st = await call(ws, 6, "Runtime.evaluate",
                            {"expression": STATE_JS, "returnByValue": True})
            png = await call(ws, 7, "Page.captureScreenshot", {"format": "png"})
            with open(out, "wb") as f:
                f.write(base64.b64decode(png["data"]))
            return json.loads(st["result"]["value"])
    finally:
        try:
            proc.terminate(); proc.wait(timeout=5)
        except Exception:
            pass


def main():
    a = sys.argv[1:]
    if len(a) < 2:
        print(__doc__); return
    url, out = a[0], a[1]
    click = None
    evals = []
    wait = 9000
    i = 2
    while i < len(a):
        if a[i] == "--click" and i + 1 < len(a):
            click = a[i + 1]; i += 2; continue
        if a[i] == "--eval" and i + 1 < len(a):
            evals.append(a[i + 1]); i += 2; continue
        if a[i] == "--wait" and i + 1 < len(a):
            wait = int(a[i + 1]); i += 2; continue
        i += 1

    st = asyncio.run(run(url, out, click, evals, wait))
    print(json.dumps(st, ensure_ascii=False, indent=1))
    print(f"\n[ok] {out}  {os.path.getsize(out)/1024:.0f} KB")


if __name__ == "__main__":
    main()
