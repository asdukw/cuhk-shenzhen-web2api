"""Task: map the AI chat API surface from within a logged-in browser session.

Merges the old probe_chat + probe_chat2 recon into one runnable script:
  1. ensure a browser session on /chat/ (login if needed)
  2. session/config reads: /.auth/me, quota pools, model catalog
  3. extract page assets (script/link urls)
  4. grep the frontend bundles for API endpoints + download bundles locally

Outputs land in data/chat_session/ (api_surface.json, chat_assets.json,
config.json, big_bundle_scan.json, bundles/*.js).

Usage:
    python src/cuhk_shenzhen_web2api/scripts/probe.py [--resume SID] [--force]
"""

from __future__ import annotations

import base64
import gzip
import json
import re
import sys
from pathlib import Path

from firecrawl import Firecrawl

from cuhk_shenzhen_web2api import cloud_browser, env, login
from cuhk_shenzhen_web2api.chat_client import ChatClient
from cuhk_shenzhen_web2api.paths import (
    BUNDLES_DIR,
    CHAT_DATA_DIR,
    SESSION_ID_FILE,
)

ALL_BUNDLES = [
    "/chat/assets/index-Dbge6LC4.js",
    "/chat/assets/ChatPage-B_vIx12K.js",
    "/chat/assets/ChatMessageArea-CGFGcUfJ.js",
    "/chat/assets/readChatDeltaStream-CWeDpR-X.js",
    "/chat/assets/chatExport-CzydR-ke.js",
    "/chat/assets/ChatArea-Bsx356Cy.js",
    "/chat/assets/runtimePaths-CjO2teYh.js",
    "/chat/assets/http-CuqGjwn9.js",
    "/chat/assets/configStore-Ckm5b5Pq.js",
    "/chat/assets/announcements-D7RF5FuE.js",
    "/chat/assets/Doubao-B8Zo4Qq8.js",
]

SMALL_BUNDLES = [
    "/chat/assets/ChatPage-B_vIx12K.js",
    "/chat/assets/ChatMessageArea-CGFGcUfJ.js",
    "/chat/assets/readChatDeltaStream-CWeDpR-X.js",
    "/chat/assets/chatExport-CzydR-ke.js",
    "/chat/assets/runtimePaths-CjO2teYh.js",
    "/chat/assets/http-CuqGjwn9.js",
    "/chat/assets/configStore-Ckm5b5Pq.js",
    "/chat/assets/announcements-D7RF5FuE.js",
    "/chat/assets/Doubao-B8Zo4Qq8.js",
]

BIG_BUNDLES = [
    "/chat/assets/index-Dbge6LC4.js",
    "/chat/assets/ChatArea-Bsx356Cy.js",
]


def step_session(client: ChatClient) -> None:
    who = client.whoami()
    print("\n=== /.auth/me ===")
    print(" ", json.dumps(who, ensure_ascii=False)[:300])

    pools = client.quota_pools()
    print("=== quota pools ===")
    print(" ", json.dumps(pools, ensure_ascii=False)[:300])

    cfg = client.config()
    body = cfg.get("body", {})
    (CHAT_DATA_DIR / "config.json").write_text(
        json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"=== /config/ ===  ({len(body.get('availableModels', []))} models)")
    print(" ", ", ".join(body.get("availableModels", [])))
    print(" ", f"config.json saved -> {CHAT_DATA_DIR / 'config.json'}")


def step_assets(cb: cloud_browser.CloudBrowser) -> None:
    out = CHAT_DATA_DIR / "chat_assets.json"
    scan = cb.page_scan()
    (CHAT_DATA_DIR / "chat_assets.json").write_text(
        json.dumps(
            {"scripts": scan.get("scripts", []), "links": scan.get("links", [])},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n=== page assets === ({len(scan.get('scripts', []))} scripts)")
    print(" ", *[p.rsplit("/", 1)[-1] for p in scan.get("scripts", [])], sep="\n   ")
    print(f"  saved -> {out.name}")


def step_bundle_surface(cb: cloud_browser.CloudBrowser, force: bool) -> None:
    out = CHAT_DATA_DIR / "api_surface.json"
    if out.exists() and not force:
        print(f"\n=== bundle API surface === (cached {out.name})")
        return

    patterns = [
        "/api/",
        "/message",
        "/conversation",
        "stream",
        "EventSource",
        "client_id",
        "authorization",
        "Bearer",
        "quota",
        "/chat/assets",
    ]
    pat_alt = "|".join(re.escape(p) for p in patterns).replace("/", r"\/")
    code = f"""
      var __res = {{}};
      var __list = {json.dumps(ALL_BUNDLES)};
      var __pat = /(?:{pat_alt})[\\w/.\\-?=&]*/g;
      var __cap = function(x, n) {{ return (x.length>n ? x.slice(0,n)+"..." : x).replace(/\\s+/g," "); }};
      var __i = 0, __b, __t, __m2, __hits, __seen, __ct;
      for (__i = 0; __i < __list.length; __i++) {{
        __b = __list[__i];
        try {{
          __t = await page.evaluate(async function(u) {{
            var r2 = await fetch(u, {{credentials:"include"}});
            if (!r2.ok) return "HTTP_" + r2.status;
            return await r2.text();
          }}, __b);
        }} catch(e) {{ __t = "ERR:" + e; }}
        if (typeof __t !== "string") __t = JSON.stringify(__t);
        __hits = []; __seen = {{}}; __ct = 0;
        __pat.lastIndex = 0;
        while ((__m2 = __pat.exec(__t)) !== null && __ct < 40) {{
          var h = __cap(__m2[0], 120);
          if (!__seen[h]) {{ __seen[h] = 1; __hits.push(__m2.index + ": " + h); __ct++; }}
        }}
        __res[__b] = {{len: __t.length, hits: __hits.slice(0, 12)}};
      }}
      JSON.stringify(__res)
    """
    raw = cb.js(code, timeout=180)
    try:
        surf = json.loads(raw)
        for b, info in surf.items():
            print(f"\n=== {b} len={info.get('len')} hits={len(info.get('hits', []))}")
            for h in info.get("hits", []):
                print("   ", h[:300])
        out.write_text(json.dumps(surf, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  saved -> {out.name}")
    except json.JSONDecodeError:
        print("  bundle surface parse failed:", raw[:800])


def step_download_bundles(cb: cloud_browser.CloudBrowser, force: bool) -> None:
    BUNDLES_DIR.mkdir(parents=True, exist_ok=True)
    cached = {p.name for p in BUNDLES_DIR.glob("*.js")}
    todo = [u for u in SMALL_BUNDLES if Path(u).name not in cached]
    if not todo and not force:
        print(f"\n=== bundle download === (cached {len(cached)} files, skipping)")
        return
    if force:
        todo = SMALL_BUNDLES

    code = f"""
      var __l = {json.dumps(todo)};
      var __chunks = [];
      for (var __i = 0; __i < __l.length; __i++) {{
        var __u = __l[__i];
        try {{
          var __txt = await page.evaluate(async function(u) {{
            var r = await fetch(u, {{credentials:"include"}});
            if (!r.ok) return "HTTP_" + r.status;
            return await r.text();
          }}, __u);
        }} catch(e) {{ __txt = "ERR:" + e; }}
        var __gz = (await import("zlib")).gzipSync(Buffer.from(__txt, "utf8"));
        __chunks.push(__u + "\\x00" + __gz.toString("base64"));
      }}
      JSON.stringify({{count: __chunks.length, chunks: __chunks}})
    """
    raw = cb.js(code, timeout=240)
    try:
        data = json.loads(raw)
        for c in data["chunks"]:
            path, b64 = c.split("\x00", 1)
            raw_bytes = gzip.decompress(base64.b64decode(b64))
            file = BUNDLES_DIR / Path(path).name
            try:
                file.write_text(raw_bytes.decode("utf-8"), encoding="utf-8")
                print(f"   -> {file.name} ({len(raw_bytes)} bytes)")
            except Exception as e:  # noqa: BLE001 - report but keep going
                print(f"   !! {path}: {e}")
    except json.JSONDecodeError:
        print("  bundle download parse failed:", raw[:800])


def step_big_scan(cb: cloud_browser.CloudBrowser, force: bool) -> None:
    out = CHAT_DATA_DIR / "big_bundle_scan.json"
    if out.exists() and not force:
        print(f"\n=== big bundle scan === (cached {out.name})")
        return

    code = f"""
      var __l = {json.dumps(BIG_BUNDLES)};
      var __out = {{}};
      var __i2;
      for (__i2 = 0; __i2 < __l.length; __i2++) {{
        var __u = __l[__i2];
        try {{
          var __t = await page.evaluate(async function(u) {{
            var r = await fetch(u, {{credentials:"include"}});
            if (!r.ok) return "HTTP_" + r.status;
            return await r.text();
          }}, __u);
        }} catch(e) {{ __t = "ERR:" + e; }}
        if (typeof __t !== "string") __t = JSON.stringify(__t);
        var __all = [];
        var __re1 = /(["'])(\\/[a-zA-Z0-9_\\-\\.\\/]{{2,}})\\1/g, __m;
        while ((__m = __re1.exec(__t)) !== null) __all.push(__m[2]);
        var __re2 = /(?:fetch\\(|\\w+Use\\w*\\(\\s*)(["']\\/[^"']*["'])|([A-Z_]+"?\\s*[:=]"?\\s*["']\\/[^"']*["'])/g, __m2;
        while ((__m2 = __re2.exec(__t)) !== null) __all.push((__m2[1] || __m2[2] || ""));
        var __re3 = /(api\\/[a-zA-Z0-9_\\-\\/]+|conversation[a-zA-Z0-9_\\-]*|send[a-zA-Z0-9_\\-]*Message|create[a-zA-Z0-9_\\-]*Conversation[a-zA-Z0-9_\\-]*)/g, __m3;
        while ((__m3 = __re3.exec(__t)) !== null) __all.push(__m3[1]);
        var __seen = new Set(), __keep = [];
        for (var __a of __all) {{ if (!__seen.has(__a)) {{ __seen.add(__a); __keep.push(__a); }} }}
        __out[__u] = {{len: __t.length, found: __keep.slice(0, 200)}};
      }}
      JSON.stringify(__out)
    """
    raw = cb.js(code, timeout=240)
    try:
        data = json.loads(raw)
        for b, info in data.items():
            print(f"\n=== {b} len={info['len']}")
            for f in info["found"]:
                print("   ", f[:160])
        out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  saved -> {out.name}")
    except json.JSONDecodeError:
        print("  big scan parse failed:", raw[:800])


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--resume", default=None, help="Resume existing Firecrawl browser sid"
    )
    parser.add_argument(
        "--force", action="store_true", help="Re-download despite cached artifacts"
    )
    args = parser.parse_args()

    config = env.load_env()
    api_key = env.firecrawl_api_key(config)
    if not api_key:
        print("FIRECRAWL_API_KEY missing in .env", file=sys.stderr)
        sys.exit(1)

    app = Firecrawl(api_key=api_key)
    sid = args.resume or cloud_browser.load_session_id(SESSION_ID_FILE)
    if sid:
        cb = cloud_browser.resume_session(app, sid)
        print("session   ", sid, flush=True)
    else:
        cb = cloud_browser.create_session(app)
        print("created   ", cb.sid, flush=True)

    final = login.ensure_on_chat(
        cb, env.chat_username(config), env.chat_password(config)
    )
    if "/chat" not in (final or ""):
        print("not on /chat/:", final, file=sys.stderr)
        sys.exit(1)

    client = ChatClient(cb)
    step_session(client)
    step_assets(cb)
    step_bundle_surface(cb, args.force)
    step_download_bundles(cb, args.force)
    step_big_scan(cb, args.force)
    print("\ndone.")


if __name__ == "__main__":
    main()
