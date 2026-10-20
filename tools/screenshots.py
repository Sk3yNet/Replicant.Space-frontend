"""Capture a screenshot of every page into docs/screens (the README's Screenshots section).

Start the mock game server and the app first (README, Local development), give the app half a minute to sync, then:
    python tools/screenshots.py [--base http://127.0.0.1:8000] [--out docs/screens]
Needs Playwright (`pip install playwright`, then `playwright install chromium` or set PW_CHROMIUM to a Chromium binary).
"""
import argparse
import os
import re

from playwright.sync_api import sync_playwright

# (file name, URL); the device, replicant and system pages are added from the device list
PAGES = [
    ("dashboard", "/"), ("tree", "/tree"), ("device-list", "/fleet"), ("ami", "/ami"), ("lost", "/lost"),
    ("galaxy-map", "/map"), ("stars", "/stars"), ("systems", "/systems"), ("prospects", "/systems/prospects"),
    ("relays", "/map/relays"), ("traffic", "/traffic"), ("trail", "/trail"), ("wards", "/wards"), ("defense", "/defence"),
    ("terraforming", "/terraforming"), ("terraforming-candidates", "/terraforming/candidates"), ("upkeep", "/maintenance"),
    ("fleets", "/fleets"), ("fleet-details", "/fleets/details"), ("logistics", "/fleets/logistics"),
    ("reform", "/fleets/reform"), ("blueprints", "/blueprints"), ("contracts", "/game-events"),
    ("reputation", "/reputation"), ("shop", "/shop"), ("simulations", "/simulations"), ("lockers", "/lockers"),
    ("automations", "/automations"), ("events", "/events"), ("messages", "/messages"),
    ("notifications", "/notifications"), ("leaderboards", "/leaderboards"), ("blog", "/blog"),
    ("account", "/account"), ("diagnostics", "/diagnostics"), ("console", "/console"),
]
MAX_HEIGHT = 1800  # long pages are cut here


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--out", default="docs/screens")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=os.environ.get("PW_CHROMIUM") or None)
        pg = b.new_page(viewport={"width": 1440, "height": 900}, color_scheme="dark")
        pg.goto(a.base + "/fleet", wait_until="networkidle")
        html = pg.content()
        pages = list(PAGES)
        for name, pat in (("device", r'href="(/devices/[0-9A-F]{8})"'), ("replicant", r'href="(/replicants/[0-9A-F]{8})"'),
                          ("system", r'href="(/systems/[A-Z][A-Z0-9-]+)"')):
            if m := re.search(pat, html):
                pages.append((name, m.group(1)))
        for name, url in pages:
            r = pg.goto(a.base + url, wait_until="networkidle")
            if name == "tree":
                pg.click("text=Expand all")
            pg.wait_for_timeout(1200)
            h = min(pg.evaluate("document.documentElement.scrollHeight"), MAX_HEIGHT)
            pg.screenshot(path=f"{a.out}/{name}.jpg", type="jpeg", quality=78, full_page=True,
                          clip={"x": 0, "y": 0, "width": 1440, "height": h})
            print(f"{name:24} {url:28} {r.status}")
        b.close()


if __name__ == "__main__":
    main()
