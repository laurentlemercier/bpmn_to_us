"""Rendu HTML pour l'affichage (sous-ensemble de HTML/CSS compris par QTextBrowser)."""
from __future__ import annotations

import html

from markdown_it.renderer import RendererHTML

from .core import Document, callout_style, make_parser

try:
    from pygments import highlight
    from pygments.formatters import HtmlFormatter
    from pygments.lexers import TextLexer, get_lexer_by_name
    from pygments.util import ClassNotFound
except ImportError:  # pygments est optionnel
    highlight = None

CSS = """
body { font-family: 'Segoe UI', Arial, sans-serif; font-size: 11pt; color: #222; }
h1 { font-size: 22pt; color: #1f3a5f; } h2 { font-size: 17pt; color: #1f3a5f; }
h3 { font-size: 14pt; color: #1f3a5f; } h4, h5, h6 { font-size: 12pt; color: #1f3a5f; }
a { color: #1a6fd4; } blockquote { color: #555; }
code { font-family: Consolas, 'DejaVu Sans Mono', monospace; background-color: #eef0f3; }
th { background-color: #e9eef6; }
"""


def _highlight(code: str, lang: str) -> str:
    if highlight is None:
        return html.escape(code)
    try:
        lexer = get_lexer_by_name(lang) if lang else TextLexer()
    except ClassNotFound:
        lexer = TextLexer()
    return highlight(code, lexer, HtmlFormatter(noclasses=True, nowrap=True))


class QtHTMLRenderer(RendererHTML):
    """Ajustements pour QTextBrowser (pas de CSS moderne : on utilise des tables)."""

    def front_matter(self, tokens, idx, options, env):
        return ""

    def table_open(self, tokens, idx, options, env):
        return '<table border="1" cellspacing="0" cellpadding="5" style="border-color:#b8b8b8;">\n'

    def mark_open(self, tokens, idx, options, env):
        return '<span style="background-color:#fff3a0;">'

    def mark_close(self, tokens, idx, options, env):
        return "</span>"

    def blockquote_open(self, tokens, idx, options, env):
        c = tokens[idx].meta.get("callout")
        env.setdefault("_bq", []).append(bool(c))
        if not c:
            return "<blockquote>\n"
        icon, color, bg = callout_style(c["type"])
        return (
            '<table width="100%" cellspacing="0" cellpadding="0"><tr>'
            f'<td width="5" bgcolor="#{color}"></td><td bgcolor="#{bg}">'
            '<table width="100%" cellspacing="0" cellpadding="8"><tr><td>'
            f'<b style="color:#{color};">{icon} {html.escape(c["title"])}</b><br/>'
        )

    def blockquote_close(self, tokens, idx, options, env):
        is_callout = env["_bq"].pop()
        return "</td></tr></table></td></tr></table>\n" if is_callout else "</blockquote>\n"

    def fence(self, tokens, idx, options, env):
        tok = tokens[idx]
        lang = (tok.info or "").strip().split(" ")[0]
        code = _highlight(tok.content.rstrip("\n"), lang)
        return (
            '<table width="100%" cellspacing="0" cellpadding="8"><tr>'
            '<td bgcolor="#f6f8fa"><pre style="font-family:Consolas,\'DejaVu Sans Mono\','
            f'monospace; font-size:10pt;">{code}</pre></td></tr></table>\n'
        )

    code_block = fence


def render_html(doc: Document) -> str:
    md = make_parser(doc.obsidian, renderer_cls=QtHTMLRenderer)
    body = md.renderer.render(doc.tokens, md.options, {})
    return f"<html><head><style>{CSS}</style></head><body>{body}</body></html>"
