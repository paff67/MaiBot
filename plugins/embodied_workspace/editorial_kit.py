from __future__ import annotations

from pathlib import Path

EDITORIAL_THEME_CSS = """/* MaiBot Fairy Editorial & Webapp Theme Kit */
:root {
  --bg: #faf8f5;
  --surface: #ffffff;
  --fg: #1c1917;
  --muted: #78716c;
  --border: #e7e5e4;
  --accent: #4f46e5;
  --accent-soft: rgba(79, 70, 229, 0.08);
  --code-bg: #f5f5f4;
  --shadow: 0 4px 24px rgba(28, 25, 23, 0.05);
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0b0f19;
    --surface: #111827;
    --fg: #f3f4f6;
    --muted: #9ca3af;
    --border: #1f2937;
    --accent: #818cf8;
    --accent-soft: rgba(129, 140, 248, 0.12);
    --code-bg: #1e293b;
    --shadow: 0 4px 24px rgba(0, 0, 0, 0.35);
  }
}
* { box-sizing: border-box; }
body {
  background: var(--bg);
  color: var(--fg);
  font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Noto Sans CJK SC", sans-serif;
  font-size: 16.5px;
  line-height: 1.82;
  margin: 0;
  padding: 28px 18px 80px;
  max-width: 740px;
  margin-left: auto;
  margin-right: auto;
  overflow-x: hidden;
  word-wrap: break-word;
}
#fairy-progress-bar {
  position: fixed; top: 0; left: 0; height: 3px; width: 0%;
  background: linear-gradient(90deg, var(--accent), #ec4899);
  z-index: 99990; transition: width 0.1s ease-out;
}
.editorial-header {
  margin-bottom: 2rem; padding-bottom: 1.25rem; border-bottom: 1px solid var(--border);
}
.editorial-badge {
  display: inline-block; font-size: 12px; font-weight: 600; color: var(--accent);
  background: var(--accent-soft); padding: 3px 10px; border-radius: 999px; margin-bottom: 10px;
}
h1 { font-size: 1.6rem; line-height: 1.35; font-weight: 800; margin: 0 0 0.6rem 0; }
.editorial-meta { font-size: 0.84rem; color: var(--muted); display: flex; flex-wrap: wrap; gap: 12px; }
h2 {
  font-size: 1.28rem; margin-top: 2.2rem; margin-bottom: 0.9rem;
  padding-left: 12px; border-left: 4px solid var(--accent); line-height: 1.4;
}
h3 { font-size: 1.1rem; margin-top: 1.6rem; margin-bottom: 0.6rem; }
p { margin: 0 0 1.15rem 0; }
.callout, blockquote {
  margin: 1.4rem 0; padding: 1rem 1.2rem;
  border-left: 4px solid var(--accent);
  background: var(--surface);
  border-radius: 0 12px 12px 0;
  box-shadow: var(--shadow);
}
pre {
  position: relative; background: var(--code-bg); padding: 14px 16px;
  border-radius: 10px; border: 1px solid var(--border);
  overflow-x: auto; font-size: 0.88rem; line-height: 1.6;
}
code {
  font-family: "JetBrains Mono", "SFMono-Regular", Consolas, monospace;
  background: var(--code-bg); padding: 2px 6px; border-radius: 5px; font-size: 0.88em;
}
pre code { background: transparent; padding: 0; }
table {
  border-collapse: collapse; width: 100%; margin: 1.4rem 0;
  font-size: 0.92rem; background: var(--surface); border-radius: 10px;
  overflow-x: auto; display: block; box-shadow: var(--shadow);
}
th, td { border: 1px solid var(--border); padding: 10px 12px; text-align: left; }
th { background: var(--accent-soft); font-weight: 600; }
.editorial-footer {
  margin-top: 3rem; padding-top: 1.2rem; border-top: 1px solid var(--border);
  font-size: 0.85rem; color: var(--muted); display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px;
}
.fairy-btn {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 7px 14px; border-radius: 8px; font-size: 13px; font-weight: 600;
  background: var(--surface); color: var(--fg); border: 1px solid var(--border);
  text-decoration: none; cursor: pointer;
}
"""

EDITORIAL_KIT_JS = r"""/* MaiBot Fairy Editorial Interactive Kit (Progress + Auto TOC + Code Copy) */
(function() {
  function initEditorialKit() {
    if (!document.getElementById("fairy-progress-bar")) {
      const bar = document.createElement("div");
      bar.id = "fairy-progress-bar";
      document.body.prepend(bar);
      window.addEventListener("scroll", () => {
        const docH = document.documentElement.scrollHeight - window.innerHeight;
        const pct = docH > 0 ? Math.min(100, Math.max(0, (window.scrollY / docH) * 100)) : 0;
        bar.style.width = pct + "%";
      }, { passive: true });
    }

    document.querySelectorAll("pre").forEach((pre) => {
      if (pre.classList.contains("mermaid")) {
        let txt = pre.textContent || "";
        txt = txt.replace(/--\s*"?([^"\n-]+?)"?\s*-->/g, "-->|$1|");
        txt = txt.replace(/\["([^"]*?)'([^"]*?)"\]/g, '["$1$2"]');
        pre.textContent = txt;
        return;
      }
      if (pre.querySelector(".fairy-copy-btn")) return;
      const btn = document.createElement("button");
      btn.className = "fairy-copy-btn";
      btn.textContent = "复制";
      btn.style.cssText = "position:absolute;top:8px;right:8px;padding:3px 8px;font-size:11px;border-radius:5px;border:1px solid var(--border);background:var(--surface);color:var(--muted);cursor:pointer";
      btn.onclick = async () => {
        const code = pre.querySelector("code") ? pre.querySelector("code").innerText : pre.innerText;
        try { await navigator.clipboard.writeText(code); btn.textContent = "已复制!"; setTimeout(() => btn.textContent = "复制", 1500); } catch (_) {}
      };
      pre.appendChild(btn);
    });

    const headings = Array.from(document.querySelectorAll("h2, h3"));
    if (headings.length >= 3 && !document.getElementById("fairy-toc-toggle")) {
      headings.forEach((h, i) => { if (!h.id) h.id = "section-" + (i + 1); });
      const btn = document.createElement("div");
      btn.id = "fairy-toc-toggle";
      btn.textContent = "📑 目录";
      btn.style.cssText = "position:fixed;bottom:52px;right:14px;z-index:99990;background:var(--surface);color:var(--fg);border:1px solid var(--border);padding:6px 12px;border-radius:999px;font-size:12px;font-weight:600;box-shadow:0 4px 12px rgba(0,0,0,.15);cursor:pointer";
      const panel = document.createElement("div");
      panel.style.cssText = "display:none;position:fixed;bottom:90px;right:14px;z-index:99991;background:var(--surface);color:var(--fg);border:1px solid var(--border);padding:12px 14px;border-radius:12px;max-width:260px;max-height:55vh;overflow-y:auto;box-shadow:0 10px 28px rgba(0,0,0,.22);font-size:13px";
      panel.innerHTML = "<div style='font-weight:700;margin-bottom:8px;border-bottom:1px solid var(--border);padding-bottom:4px'>目录</div>" +
        headings.map(h => `<a href="#${h.id}" style="display:block;padding:4px 0;color:var(--fg);text-decoration:none;padding-left:${h.tagName==='H3'?'12px':'0'};font-size:${h.tagName==='H3'?'12px':'13px'}">${h.textContent}</a>`).join("");
      btn.onclick = () => { panel.style.display = panel.style.display === "none" ? "block" : "none"; };
      panel.onclick = (e) => { if (e.target.tagName === "A") panel.style.display = "none"; };
      document.body.appendChild(btn);
      document.body.appendChild(panel);
    }
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", initEditorialKit);
  else initEditorialKit();
})();
"""


def ensure_maibot_kit_assets(ws_dir: Path, dist_dir: Path | None = None) -> Path:
    kit_dir = ws_dir / ".maibot_kit"
    kit_dir.mkdir(parents=True, exist_ok=True)
    (kit_dir / "editorial_theme.css").write_text(EDITORIAL_THEME_CSS, encoding="utf-8")
    (kit_dir / "editorial_kit.js").write_text(EDITORIAL_KIT_JS, encoding="utf-8")

    if dist_dir is not None:
        dist_kit = dist_dir / "_kit"
        dist_kit.mkdir(parents=True, exist_ok=True)
        (dist_kit / "editorial_theme.css").write_text(EDITORIAL_THEME_CSS, encoding="utf-8")
        (dist_kit / "editorial_kit.js").write_text(EDITORIAL_KIT_JS, encoding="utf-8")
    return kit_dir
