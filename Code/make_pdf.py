"""
make_pdf.py
===========
Generates a high-quality Persian (RTL) Report.pdf from the report content.

Method: the Markdown content (from make_report.build) is converted to styled HTML
with right-to-left CSS and a Persian font, figures are embedded as base64 (so the
HTML is self-contained), and it is printed to PDF via a headless browser
(Microsoft Edge or Google Chrome).

NOTE: the report *body* and cover text are intentionally Persian (the deliverable
report is in Persian); only the code comments/docstrings here are in English.

Run:  python make_pdf.py       (after  python main.py all)
"""

from __future__ import annotations

import base64
import html
import os
import re
import shutil
import subprocess
import sys

import config
import make_report

HTML_OUT = os.path.join(config.PROJECT_DIR, "Report.html")
PDF_OUT = os.path.join(config.PROJECT_DIR, "Report.pdf")


# ===========================================================================
# Tiny Markdown -> HTML converter (the subset used in the report)
# ===========================================================================
def _inline(text: str) -> str:
    text = html.escape(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"`(.+?)`", r'<code>\1</code>', text)
    return text


def _img_data_uri(path: str) -> str:
    full = os.path.join(config.PROJECT_DIR, path)
    if not os.path.exists(full):
        return ""
    with open(full, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    return f"data:image/png;base64,{b64}"


def md_to_html(md: str) -> str:
    lines = md.split("\n")
    out = []
    i = 0
    n = len(lines)

    def is_table_sep(s: str) -> bool:
        return bool(re.match(r"^\s*\|[\s:|-]+\|\s*$", s))

    while i < n:
        line = lines[i]

        # image (on its own line)
        m = re.match(r"^\!\[(.*?)\]\((.*?)\)\s*$", line.strip())
        if m:
            cap, src = m.group(1), m.group(2)
            uri = _img_data_uri(src)
            if uri:
                out.append(f'<figure><img src="{uri}" alt="{html.escape(cap)}"/></figure>')
            i += 1
            continue

        # heading
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            level = len(m.group(1))
            out.append(f"<h{level}>{_inline(m.group(2))}</h{level}>")
            i += 1
            continue

        # horizontal rule
        if re.match(r"^---+\s*$", line):
            out.append("<hr/>")
            i += 1
            continue

        # blockquote (block)
        if line.startswith(">"):
            buf = []
            while i < n and lines[i].startswith(">"):
                buf.append(_inline(lines[i].lstrip(">").strip()))
                i += 1
            out.append("<blockquote>" + "<br/>".join(buf) + "</blockquote>")
            continue

        # table
        if line.strip().startswith("|") and i + 1 < n and is_table_sep(lines[i + 1]):
            header = [c.strip() for c in line.strip().strip("|").split("|")]
            i += 2
            body = []
            while i < n and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                body.append(cells)
                i += 1
            th = "".join(f"<th>{_inline(c)}</th>" for c in header)
            trs = []
            for row in body:
                tds = "".join(f"<td>{_inline(c)}</td>" for c in row)
                trs.append(f"<tr>{tds}</tr>")
            out.append(f'<table><thead><tr>{th}</tr></thead>'
                       f'<tbody>{"".join(trs)}</tbody></table>')
            continue

        # list
        if re.match(r"^\s*-\s+", line):
            items = []
            while i < n and re.match(r"^\s*-\s+", lines[i]):
                items.append(f"<li>{_inline(re.sub(r'^\s*-\s+', '', lines[i]))}</li>")
                i += 1
            out.append("<ul>" + "".join(items) + "</ul>")
            continue

        # blank line
        if line.strip() == "":
            i += 1
            continue

        # paragraph
        out.append(f"<p>{_inline(line)}</p>")
        i += 1

    return "\n".join(out)


# ===========================================================================
# HTML template with right-to-left CSS and a Persian font
# ===========================================================================
CSS = """
@page { size: A4; margin: 1.7cm 1.5cm; }
* { box-sizing: border-box; }
body {
  font-family: Tahoma, "Segoe UI", "Vazirmatn", "IRANSans", sans-serif;
  direction: rtl; text-align: right; color: #1f2933; line-height: 1.85;
  font-size: 11.5pt; margin: 0;
}
h1 { font-size: 22pt; color: #12324f; margin: 0 0 4px; line-height: 1.4; }
h2 {
  font-size: 15.5pt; color: #0b66c3; margin: 22px 0 8px;
  border-bottom: 2px solid #d7e6f5; padding-bottom: 5px;
}
h3 { font-size: 12.8pt; color: #12324f; margin: 16px 0 6px; }
p { margin: 7px 0; }
hr { border: none; border-top: 1px solid #e2e8f0; margin: 18px 0; }
ul { margin: 6px 26px 6px 0; padding: 0; }
li { margin: 4px 0; }
code {
  direction: ltr; unicode-bidi: embed; font-family: Consolas, "Courier New", monospace;
  background: #f1f5f9; color: #b5327a; padding: 1px 5px; border-radius: 4px;
  font-size: 10.5pt;
}
blockquote {
  background: #f0f7ff; border-right: 4px solid #0b66c3; margin: 12px 0;
  padding: 9px 14px; border-radius: 6px; color: #234; font-size: 11pt;
}
table {
  border-collapse: collapse; width: 100%; margin: 12px 0; font-size: 10.5pt;
  page-break-inside: avoid;
}
th, td { border: 1px solid #cbd5e1; padding: 6px 8px; text-align: center; }
th { background: #0b66c3; color: #fff; font-weight: bold; }
tbody tr:nth-child(even) { background: #f4f8fc; }
td:last-child, th:last-child { direction: ltr; }
figure { margin: 14px 0; text-align: center; page-break-inside: avoid; }
img { max-width: 100%; border: 1px solid #e2e8f0; border-radius: 8px;
      box-shadow: 0 2px 6px rgba(0,0,0,.08); }
.cover {
  background: linear-gradient(135deg, #0b66c3, #12324f); color: #fff;
  padding: 26px 30px; border-radius: 14px; margin-bottom: 20px;
}
.cover h1 { color: #fff; }
.cover .sub { font-size: 12.5pt; opacity: .95; margin-top: 8px; }
.cover .meta { font-size: 10.5pt; opacity: .85; margin-top: 14px; }
h2 { page-break-after: avoid; }
"""


def build_html() -> str:
    md = make_report.build()
    body_html = md_to_html(md)
    # Persian cover block (report content, kept in Persian on purpose)
    cover = (
        '<div class="cover">'
        '<h1>قاصدک در برابر جاسوس</h1>'
        '<div class="sub">شبیه‌سازی پویا و چالش حریم خصوصی در شبکه</div>'
        '<div class="meta">درس شبکه‌های کامپیوتری (۴۰۴۴۳)</div>'
        '</div>'
    )
    # remove the first duplicate h1 produced from the Markdown
    body_html = re.sub(r"<h1>.*?</h1>", "", body_html, count=1, flags=re.S)
    return (
        "<!DOCTYPE html><html dir='rtl' lang='fa'><head><meta charset='utf-8'>"
        f"<style>{CSS}</style></head><body>{cover}{body_html}</body></html>"
    )


# ===========================================================================
# Find a headless browser and print to PDF
# ===========================================================================
def _find_browser():
    candidates = [
        os.environ.get("EDGE_PATH"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    for name in ("msedge", "chrome", "chromium", "google-chrome"):
        p = shutil.which(name)
        if p:
            return p
    return None


def html_to_pdf(html_path: str, pdf_path: str) -> bool:
    browser = _find_browser()
    if not browser:
        print("!! Edge/Chrome not found. Report.html was created; open it in a "
              "browser and use Print -> Save as PDF.")
        return False
    url = "file:///" + html_path.replace("\\", "/")
    tmp_profile = os.path.join(config.PROJECT_DIR, ".edge_pdf_profile")
    cmd = [
        browser, "--headless=new", "--disable-gpu", "--no-sandbox",
        "--no-first-run", "--disable-extensions",
        f"--user-data-dir={tmp_profile}",
        "--print-to-pdf-no-header", f"--print-to-pdf={pdf_path}", url,
    ]
    try:
        subprocess.run(cmd, timeout=120, capture_output=True)
    except Exception as e:
        print(f"!! browser run failed: {e}")
        return False
    finally:
        shutil.rmtree(tmp_profile, ignore_errors=True)
    return os.path.exists(pdf_path)


if __name__ == "__main__":
    config.ensure_dirs()
    html_doc = build_html()
    with open(HTML_OUT, "w", encoding="utf-8") as f:
        f.write(html_doc)
    print(f"HTML created: {HTML_OUT}")
    ok = html_to_pdf(HTML_OUT, PDF_OUT)
    if ok:
        size = os.path.getsize(PDF_OUT) / 1024
        print(f"OK PDF generated: {PDF_OUT}  ({size:.0f} KB)")
    else:
        sys.exit(1)
