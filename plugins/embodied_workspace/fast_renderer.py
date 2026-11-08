from __future__ import annotations

import html
import markdown


def render_markdown_to_wechat_html(title: str, markdown_text: str) -> str:
    safe_title = html.escape(title)
    body_html = markdown.markdown(
        markdown_text,
        extensions=["fenced_code", "tables", "nl2br", "sane_lists"],
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
  <meta name="robots" content="noindex, nofollow, noarchive">
  <meta property="og:title" content="{safe_title}">
  <title>{safe_title}</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.css">
  <style>
    :root {{ --bg: #ffffff; --fg: #1e293b; --muted: #64748b; --code-bg: #f1f5f9; --border: #e2e8f0; }}
    @media (prefers-color-scheme: dark) {{
      :root {{ --bg: #0f172a; --fg: #e2e8f0; --muted: #94a3b8; --code-bg: #1e293b; --border: #334155; }}
    }}
    body {{
      background: var(--bg); color: var(--fg);
      font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif;
      line-height: 1.75; margin: 0; padding: 20px 16px 48px;
      max-width: 720px; margin-left: auto; margin-right: auto;
      overflow-x: hidden; word-wrap: break-word;
    }}
    h1 {{ font-size: 1.45rem; line-height: 1.35; margin-bottom: 1rem; border-bottom: 1px solid var(--border); padding-bottom: 0.6rem; }}
    h2 {{ font-size: 1.2rem; margin-top: 1.8rem; }}
    pre {{ background: var(--code-bg); padding: 12px; border-radius: 8px; overflow-x: auto; font-size: 0.88rem; }}
    code {{ background: var(--code-bg); padding: 2px 5px; border-radius: 4px; font-size: 0.9em; }}
    table {{ border-collapse: collapse; width: 100%; margin: 1rem 0; font-size: 0.92rem; display: block; overflow-x: auto; }}
    th, td {{ border: 1px solid var(--border); padding: 8px 10px; text-align: left; }}
    blockquote {{ margin: 1rem 0; padding: 0.5rem 1rem; border-left: 4px solid #6366f1; background: var(--code-bg); border-radius: 0 8px 8px 0; }}
  </style>
</head>
<body>
  <article>
    <h1>{safe_title}</h1>
    {body_html}
  </article>
  <script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.js"></script>
  <script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/contrib/auto-render.min.js"
    onload="renderMathInElement(document.body, {{delimiters:[{{left:'$$',right:'$$',display:true}},{{left:'$',right:'$',display:false}}]}});"></script>
  <script type="module">
    import mermaid from 'https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.esm.min.mjs';
    mermaid.initialize({{ startOnLoad: false }});
    await mermaid.run({{ querySelector: 'code.language-mermaid, .mermaid' }});
  </script>
</body>
</html>"""
