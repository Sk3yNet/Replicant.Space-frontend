"""Save the game docs (every /docs/ page linked from the docs index, plus the changelog) as plain text:
`python tools/fetch_docs.py <out_dir>`. Used to check the app against the published docs."""
import re, html, sys, subprocess, os
out_dir = sys.argv[1] if len(sys.argv) > 1 else "docs_text"
os.makedirs(out_dir, exist_ok=True)
src = subprocess.run(["curl", "-sS", "-m", "20", "https://replicant.space/docs/"], capture_output=True, text=True).stdout
paths = sorted(set(re.findall(r'href="(/docs/[^"#]*)"', src)))
def text(t):
    t = re.sub(r"<script.*?</script>|<style.*?</style>|<nav.*?</nav>|<header.*?</header>|<footer.*?</footer>", "", t, flags=re.S)
    t = re.sub(r"<pre[^>]*>(.*?)</pre>", lambda m: "\n```\n" + re.sub(r"<[^>]+>", "", m.group(1)) + "\n```\n", t, flags=re.S)
    t = re.sub(r"<(br|/p|/li|/h\d|/tr|/div)[^>]*>", "\n", t)
    t = html.unescape(re.sub(r"<[^>]+>", "", t))
    return re.sub(r"\n\s*\n+", "\n\n", re.sub(r"[ \t]+", " ", t)).strip()
for p in paths + ["/changelog/"]:
    out = subprocess.run(["curl", "-sS", "-m", "20", "https://replicant.space" + p], capture_output=True, text=True).stdout
    name = p.strip("/").replace("/", "_") or "index"
    open(os.path.join(out_dir, name + ".txt"), "w").write(text(out))
print(len(paths))
