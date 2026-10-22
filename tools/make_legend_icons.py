"""Write the map legend icons for the README (docs/icons/*.svg).

Each icon redraws a galaxy or system map marker from app/rsweb/static/map.js (MARK_TEX, megaTex, shieldTex …) and
templates/partials/system_svg.html in the same colors, on a dark tile so it reads on light and dark GitHub themes.
Run it again after changing a marker: python tools/make_legend_icons.py
"""
import math
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "docs" / "icons"
BG = '<rect width="64" height="64" rx="10" fill="#0b1020"/>'


def svg(body, bg=True):
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64">'
            + (BG if bg else "") + body + "</svg>\n")


def pts(points):
    return " ".join(f"{x:.1f},{y:.1f}" for x, y in points)


def hub(color):   # three spokes with a dot on each end
    out = []
    for i in range(3):
        a = -math.pi / 2 + i * 2 * math.pi / 3
        x, y = 32 + 22 * math.cos(a), 34 + 22 * math.sin(a)
        out.append(f'<line x1="32" y1="34" x2="{x:.1f}" y2="{y:.1f}" stroke="{color}" stroke-width="5" stroke-linecap="round"/>'
                   f'<circle cx="{x:.1f}" cy="{y:.1f}" r="6" fill="{color}"/>')
    return "".join(out) + f'<circle cx="32" cy="34" r="7" fill="{color}"/>'


def shield(color):
    return (f'<path d="M32 6 L54 14 Q54 44 32 58 Q10 44 10 14 Z" fill="none" stroke="{color}" stroke-width="4" '
            'stroke-linejoin="round"/>')


def spiral():
    p = []
    for i in range(61):
        a, r = i / 60 * math.pi * 4.5, 3 + i / 60 * 25
        p.append((32 + r * math.cos(a), 32 + r * math.sin(a)))
    return f'<polyline points="{pts(p)}" fill="none" stroke="#e040fb" stroke-width="5" stroke-linecap="round" stroke-linejoin="round"/>'


def starburst():
    p = [(32 + (12 if i % 2 else 30) * math.cos(i * math.pi / 8), 32 + (12 if i % 2 else 30) * math.sin(i * math.pi / 8))
         for i in range(16)]
    return f'<polygon points="{pts(p)}" fill="#fff27a"/>'


def hexagon(r, color, fill):
    p = [(32 + r * math.cos(math.pi / 3 * i + math.pi / 6), 32 + r * math.sin(math.pi / 3 * i + math.pi / 6)) for i in range(6)]
    style = f'fill="{color}"' if fill else f'fill="none" stroke="{color}" stroke-width="4" stroke-linejoin="round"'
    return f'<polygon points="{pts(p)}" {style}/>'


def brackets():
    out = []
    for x, y, sx, sy in [(4, 4, 1, 1), (60, 4, -1, 1), (4, 60, 1, -1), (60, 60, -1, -1)]:
        out.append(f'<polyline points="{x},{y + sy * 14} {x},{y} {x + sx * 14},{y}" fill="none" stroke="#7fe3ff" stroke-width="3"/>')
    return "".join(out)


def sparkle(cx, cy, r, color):   # four-point sparkle: mining
    p = []
    for i in range(8):
        a, rr = i * math.pi / 4 - math.pi / 2, r if i % 2 == 0 else r * .28
        p.append((cx + rr * math.cos(a), cy + rr * math.sin(a)))
    return f'<polygon points="{pts(p)}" fill="{color}"/>'


def glow(cx, cy, r, color, gid, opacity=1):
    return (f'<defs><radialGradient id="{gid}"><stop offset="0" stop-color="{color}" stop-opacity="{opacity}"/>'
            f'<stop offset=".4" stop-color="{color}" stop-opacity="{opacity * .6}"/>'
            f'<stop offset="1" stop-color="{color}" stop-opacity="0"/></radialGradient></defs>'
            f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="url(#{gid})"/>')


STAR = '<circle cx="32" cy="32" r="4" fill="#fff"/>'

ICONS = {
    # galaxy map star markers
    "replicant": '<polygon points="32,6 56,32 32,58 8,32" fill="#ff5fa2"/>',
    "devices": '<rect x="13" y="13" width="38" height="38" fill="#6cb6ff"/>',
    "hub": hub("#f0b64f"),
    "other-hub": hub("#ff7a6b"),
    "ward": shield("#6cb6ff"),
    "other-ward": shield("#ff7a6b"),
    "scanned": '<circle cx="32" cy="32" r="24" fill="none" stroke="#4fd18b" stroke-width="3" stroke-dasharray="5 5"/>' + STAR,
    "found": starburst(),
    "prospected": brackets() + STAR,
    "megastructure": hexagon(24, "#c792ff", False) + hexagon(9, "#c792ff", True),
    "rich": ('<polygon points="14,22 22,10 42,10 50,22 32,56" fill="#ffa726"/>'
             '<path d="M14 22 L50 22 M26 22 L32 56 L38 22" fill="none" stroke="rgba(0,0,0,.55)" stroke-width="3"/>'),
    "anomaly": spiral(),
    "hazard": (glow(32, 32, 30, "#ff3b30", "hz", .9)[:-2]   # the haze breathes, like on the map
               + '><animate attributeName="r" values="22;30;22" dur="3s" repeatCount="indefinite"/></circle>' + STAR),
    "surveying": ('<circle cx="32" cy="32" r="8" fill="none" stroke="#4fd18b" stroke-width="3">'
                  '<animate attributeName="r" values="6;28" dur="2.4s" repeatCount="indefinite"/>'
                  '<animate attributeName="opacity" values="1;0" dur="2.4s" repeatCount="indefinite"/></circle>' + STAR),
    "region": glow(24, 30, 26, "#5c6bc0", "rg", .7) + glow(42, 36, 22, "#26a69a", "rg2", .55),
    "mining": (sparkle(20, 22, 14, "#b0bec5") + sparkle(44, 20, 11, "#ffa726") + sparkle(26, 46, 10, "#4dd0e1")
               + sparkle(46, 44, 12, "#e040fb")),
    "cover": ('<circle cx="32" cy="32" r="26" fill="#6cb6ff" fill-opacity=".12" stroke="#6cb6ff" stroke-opacity=".6" '
              'stroke-width="2"/>' + STAR),
    # galaxy map lines
    "fleet": ('<path d="M8 50 Q30 14 56 18" fill="none" stroke="#3fd0c9" stroke-width="4" stroke-dasharray="7 5">'
              '<animate attributeName="stroke-dashoffset" values="24;0" dur="1s" repeatCount="indefinite"/></path>'),
    "transit": ('<line x1="8" y1="50" x2="46" y2="20" stroke="#b48cff" stroke-width="4" stroke-linecap="round"/>'
                '<polygon points="56,12 36,18 48,32" fill="#b48cff"/>'),
    "supply": ('<path d="M8 48 Q32 4 56 48" fill="none" stroke="#ffb74d" stroke-width="4"/>'
               '<path d="M8 56 Q32 24 56 56" fill="none" stroke="#6cb6ff" stroke-width="3"/>'),
    "prospecting": ('<path d="M10 54 L54 14 A40 40 0 0 1 58 30 Z" fill="#7fe3ff" fill-opacity=".25"/>'
                    '<path d="M54 14 A40 40 0 0 1 58 30" fill="none" stroke="#7fe3ff" stroke-width="3"/>'
                    '<circle cx="10" cy="54" r="4" fill="#fff"/>'),
    "relay-network": ('<path d="M8 44 Q20 20 32 30 T56 18" fill="none" stroke="#4fc3f7" stroke-width="4"/>'
                      '<path d="M32 30 Q40 48 56 52" fill="none" stroke="#2a7594" stroke-width="4"/>'
                      '<circle cx="8" cy="44" r="4" fill="#fff"/><circle cx="32" cy="30" r="4" fill="#fff"/>'
                      '<circle cx="56" cy="18" r="4" fill="#fff"/><circle cx="56" cy="52" r="4" fill="#fff"/>'),
    "relay-inactive": ('<path d="M8 44 Q20 20 32 30 T56 18" fill="none" stroke="#fff176" stroke-width="4" stroke-dasharray="6 5"/>'
                       '<circle cx="8" cy="44" r="4" fill="#fff"/><circle cx="56" cy="18" r="4" fill="#fff"/>'),
    "relay-order": '<circle cx="32" cy="32" r="18" fill="none" stroke="#fff" stroke-width="4"/>' + STAR,
    "relay-off": '<circle cx="32" cy="32" r="18" fill="none" stroke="#ff3b30" stroke-width="4"/>' + STAR,
    "relay-bridge": ('<line x1="8" y1="48" x2="56" y2="16" stroke="#9e9e9e" stroke-width="4" stroke-dasharray="6 6"/>'
                     '<circle cx="8" cy="48" r="4" fill="#fff"/><circle cx="56" cy="16" r="4" fill="#fff"/>'),
    "other-players": ('<circle cx="24" cy="36" r="14" fill="none" stroke="#ff7a6b" stroke-width="3"/>'
                      '<line x1="40" y1="20" x2="54" y2="8" stroke="#ff7a6b" stroke-width="4" stroke-linecap="round"/>'
                      '<polygon points="58,4 46,8 54,16" fill="#ff7a6b"/>'),
    "trail": ('<path d="M8 54 L22 40 L34 44 L56 12" fill="none" stroke="#ffd54f" stroke-width="4" '
              'stroke-linejoin="round" stroke-linecap="round"/>'),
    # system map
    "drone-work": ('<circle cx="32" cy="32" r="18" fill="none" stroke="#4fd18b" stroke-opacity=".35" stroke-width="2"/>'
                   '<circle cx="32" cy="32" r="5" fill="#7f95b8"/>'
                   '<g><circle cx="50" cy="32" r="5" fill="#4fd18b"/>'
                   '<animateTransform attributeName="transform" type="rotate" from="0 32 32" to="360 32 32" dur="4s" '
                   'repeatCount="indefinite"/></g>'),
    "drone-survey": ('<circle cx="32" cy="32" r="24" fill="none" stroke="#8fd8ff" stroke-opacity=".35" stroke-width="2"/>'
                     '<circle cx="32" cy="32" r="5" fill="#7f95b8"/>'
                     '<g><path d="M54 30.4 L37 28 L37 36 L54 33.6 Z" fill="#8fd8ff" fill-opacity=".45"/>'
                     '<circle cx="56" cy="32" r="5" fill="#8fd8ff"/>'
                     '<animateTransform attributeName="transform" type="rotate" from="360 32 32" to="0 32 32" dur="6s" '
                     'repeatCount="indefinite"/></g>'),
}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for name, body in ICONS.items():
        (OUT / f"{name}.svg").write_text(svg(body))
    print(f"{len(ICONS)} icons in {OUT}")


if __name__ == "__main__":
    main()
