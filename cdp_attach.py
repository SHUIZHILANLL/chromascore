#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
cdp_attach.py — 连到已运行的 WebView2 调试端口截图
    $env:WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS="--remote-debugging-port=9351"
    py cdp_attach.py 9351 out.png [--click SEL] [--eval JS]
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import urllib.request

import websockets

STATE_JS = """(() => {
  const bg = document.getElementById('bg'), viz = document.getElementById('viz');
  let px = null;
  if (bg) { try { const d = bg.getContext('2d').getImageData(
      Math.floor(bg.width/2), Math.floor(bg.height/2), 1, 1).data;
    px = [d[0], d[1], d[2], d[3]]; } catch(e) { px = 'ERR'; } }
  const r = bg ? bg.getBoundingClientRect() : null;
  return JSON.stringify({
    title: document.title, bodyClass: document.body.className,
    bodyDisplay: getComputedStyle(document.body).display,
    bodySize: [document.body.offsetWidth, document.body.offsetHeight],
    bgRect: r ? [r.x|0, r.y|0, r.width|0, r.height|0] : null,
    bgCenter: px, vizDisplay: viz ? getComputedStyle(viz).display : null,
    items: document.querySelectorAll('.item').length,
    setOpen: document.getElementById('setModal')
      ? document.getElementById('setModal').classList.contains('open') : null,
    errText: (document.getElementById('__err')||{}).textContent || null,
  });
})()"""


async def call(ws, mid, method, params=None):
    await ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
    while True:
        m = json.loads(await ws.recv())
        if m.get("id") == mid:
            if "error" in m:
                raise RuntimeError(f"{method}: {m['error']}")
            return m.get("result", {})


async def run(port, out, click, evals):
    tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=5))
    pages = [t for t in tabs if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
    if not pages:
        raise RuntimeError("no page target")
    url = pages[0]["webSocketDebuggerUrl"]

    async with websockets.connect(url, max_size=80 * 1024 * 1024) as ws:
        await call(ws, 1, "Page.enable")
        await call(ws, 2, "Runtime.enable")
        if click:
            await call(ws, 3, "Runtime.evaluate",
                       {"expression": f"document.querySelector('{click}').click()"})
            await asyncio.sleep(1.6)
        for m in evals:
            await call(ws, 4, "Runtime.evaluate", {"expression": m})
            await asyncio.sleep(1.4)
        st = await call(ws, 5, "Runtime.evaluate",
                        {"expression": STATE_JS, "returnByValue": True})
        png = await call(ws, 6, "Page.captureScreenshot", {"format": "png"})
        with open(out, "wb") as f:
            f.write(base64.b64decode(png["data"]))
        return json.loads(st["result"]["value"])


def main():
    a = sys.argv[1:]
    if len(a) < 2:
        print(__doc__); return
    port, out = int(a[0]), a[1]
    click, evals, i = None, [], 2
    while i < len(a):
        if a[i] == "--click" and i + 1 < len(a):
            click = a[i + 1]; i += 2; continue
        if a[i] == "--eval" and i + 1 < len(a):
            evals.append(a[i + 1]); i += 2; continue
        i += 1
    st = asyncio.run(run(port, out, click, evals))
    print(json.dumps(st, ensure_ascii=False, indent=1))
    print(f"\n[ok] {out}  {os.path.getsize(out)/1024:.0f} KB")


if __name__ == "__main__":
    main()
