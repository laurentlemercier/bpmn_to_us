"""
Noyau : lecture Markdown (standard + Obsidian) -> Document (tokens normalisés).

Chaîne de traitement
--------------------
    texte .md
      │  (si Obsidian) prétraitement : ![[img|300]], [[wikilink]], ![[note]],
      │                %%commentaires%%, - [ ] tâches
      ▼
    markdown-it-py (CommonMark + tables + barré + front-matter + ==surlignage==)
      ▼
    post-traitement des tokens : callouts "> [!note]", résolution des images
      ▼
    Document  ──►  html_view.render_html()   (affichage)
              └─►  docx_export.DocxExporter  (export Word)

Les deux sorties lisent exactement les mêmes tokens : ce qui s'affiche est
ce qui est exporté.
"""
from __future__ import annotations

import hashlib
import re
import tempfile
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from markdown_it import MarkdownIt
from markdown_it.renderer import RendererHTML
from markdown_it.token import Token
from mdit_py_plugins.front_matter import front_matter_plugin
from PIL import Image

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg", ".tif", ".tiff"}

OBSIDIAN_HINTS = re.compile(r"!\[\[|\[\[[^\]\n]+\]\]|^>\s*\[!\w+\]|%%|==[^=\n]+==", re.M)
EMBED_RE = re.compile(r"!\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|([^\]]*))?\]\]")
WIKI_RE = re.compile(r"(?<!!)\[\[([^\]|#\n]+)(?:#([^\]|\n]*))?(?:\|([^\]\n]*))?\]\]")
COMMENT_RE = re.compile(r"%%.*?%%", re.S)
TASK_RE = re.compile(r"^(\s*(?:[-*+]|\d+[.)])\s+)\[([ xX])\]\s", re.M)
CALLOUT_RE = re.compile(r"^\[!([\w-]+)\]([+-]?)[ \t]*([^\n]*)\n?(.*)$", re.S)
ESC_PIPE_RE = re.compile(r"(\[\[[^\]\n]*?)\\\|")   # [[a\|b]] (pipe échappé dans un tableau)
FRONT_RE = re.compile(r"\A---\r?\n.*?\r?\n---\r?\n", re.S)

# type -> (symbole, couleur du bord, couleur de fond)
CALLOUT_STYLES = {
    "note": ("ℹ", "2F6FDE", "E8F0FE"),
    "info": ("ℹ", "2F6FDE", "E8F0FE"),
    "todo": ("☐", "2F6FDE", "E8F0FE"),
    "abstract": ("≡", "00A5B5", "E0F6F8"),
    "tip": ("★", "00A58E", "E0F5F1"),
    "success": ("✔", "2EA043", "E6F4EA"),
    "question": ("?", "D9A400", "FFF8DB"),
    "warning": ("⚠", "E8890C", "FFF1DF"),
    "failure": ("✖", "D93B3B", "FDECEC"),
    "danger": ("✖", "D93B3B", "FDECEC"),
    "bug": ("✖", "D93B3B", "FDECEC"),
    "example": ("▸", "7C4DDB", "F0EAFB"),
    "quote": ("❝", "808080", "F2F2F2"),
}
CALLOUT_ALIASES = {
    "summary": "abstract", "tldr": "abstract", "hint": "tip", "important": "tip",
    "check": "success", "done": "success", "help": "question", "faq": "question",
    "caution": "warning", "attention": "warning", "fail": "failure", "missing": "failure",
    "error": "danger", "cite": "quote",
}


def callout_style(kind: str):
    kind = CALLOUT_ALIASES.get(kind, kind)
    return CALLOUT_STYLES.get(kind, CALLOUT_STYLES["note"])


# ---------------------------------------------------------------------------
# Modèle
# ---------------------------------------------------------------------------

@dataclass
class Document:
    tokens: list[Token]
    meta: dict
    warnings: list[str]
    source: Path | None
    title: str
    obsidian: bool
    base_dir: Path
    vault_root: Path | None = None

    @property
    def breaks(self) -> bool:
        """Obsidian affiche un simple retour à la ligne comme un saut de ligne."""
        return self.obsidian


# ---------------------------------------------------------------------------
# Parseur
# ---------------------------------------------------------------------------

def _mark_rule(state, silent):
    """Règle inline ==surlignage== (Obsidian)."""
    src, pos = state.src, state.pos
    if src[pos:pos + 2] != "==":
        return False
    end = src.find("==", pos + 2)
    if end == -1 or end == pos + 2:
        return False
    if not silent:
        state.push("mark_open", "mark", 1)
        state.push("text", "", 0).content = src[pos + 2:end]
        state.push("mark_close", "mark", -1)
    state.pos = end + 2
    return True


def make_parser(obsidian: bool, renderer_cls=RendererHTML) -> MarkdownIt:
    md = MarkdownIt("commonmark", {"breaks": obsidian, "html": True},
                    renderer_cls=renderer_cls)
    md.enable(["table", "strikethrough"])
    md.use(front_matter_plugin)
    if obsidian:
        md.inline.ruler.before("emphasis", "mark", _mark_rule)
    return md


def parse_front_matter(raw: str) -> dict:
    meta = {}
    for line in raw.splitlines():
        m = re.match(r"^([\w-]+):\s*(.*)$", line)
        if m:
            meta[m.group(1).lower()] = m.group(2).strip().strip("\"'")
    return meta


# ---------------------------------------------------------------------------
# Ressources (images, notes)
# ---------------------------------------------------------------------------

def find_vault_root(path: Path) -> Path | None:
    for parent in [path.parent, *path.parents]:
        if (parent / ".obsidian").is_dir():
            return parent
    return None


class AssetResolver:
    """Résout une référence d'image/de note comme le fait Obsidian :
    chemin relatif au fichier, relatif au coffre, puis recherche par nom."""

    def __init__(self, base_dir: Path, vault_root: Path | None = None):
        self.base_dir = Path(base_dir)
        self.vault_root = Path(vault_root) if vault_root else None
        self._idx: dict | None = None

    def _index(self):
        if self._idx is None:
            self._idx = {"asset": {}, "note": {}}
            root = self.vault_root or self.base_dir
            for n, f in enumerate(root.rglob("*")):
                if n > 50000:  # garde-fou sur les gros dossiers
                    break
                if not f.is_file() or any(p.startswith(".") for p in f.relative_to(root).parts):
                    continue
                ext = f.suffix.lower()
                if ext == ".md":
                    self._idx["note"].setdefault(f.stem.lower(), f.resolve())
                elif ext in IMAGE_EXT:
                    self._idx["asset"].setdefault(f.name.lower(), f.resolve())
        return self._idx

    def resolve(self, ref: str) -> Path | None:
        ref = urllib.parse.unquote(ref.strip().strip("<>"))
        p = Path(ref)
        candidates = [p] if p.is_absolute() else [self.base_dir / p]
        if self.vault_root and not p.is_absolute():
            candidates.append(self.vault_root / p)
        for c in candidates:
            if c.is_file():
                return c.resolve()
        return self._index()["asset"].get(p.name.lower())

    def resolve_note(self, name: str) -> Path | None:
        name = urllib.parse.unquote(name).strip()
        for root in (self.base_dir, self.vault_root):
            if root and (root / f"{name}.md").is_file():
                return (root / f"{name}.md").resolve()
        return self._index()["note"].get(Path(name).stem.lower())


def find_note(name: str, base_dir: Path, vault_root: Path | None) -> Path | None:
    return AssetResolver(base_dir, vault_root).resolve_note(name)


def image_size(path: Path) -> tuple[int, int]:
    if path.suffix.lower() == ".svg":
        txt = path.read_text(encoding="utf-8", errors="ignore")[:3000]
        w = re.search(r'\bwidth="([\d.]+)', txt)
        h = re.search(r'\bheight="([\d.]+)', txt)
        if w and h:
            return int(float(w.group(1))), int(float(h.group(1)))
        vb = re.search(r'viewBox="[\d.\-]+[ ,]+[\d.\-]+[ ,]+([\d.]+)[ ,]+([\d.]+)', txt)
        if vb:
            return int(float(vb.group(1))), int(float(vb.group(2)))
        return 600, 400
    with Image.open(path) as im:
        return im.size


# ---------------------------------------------------------------------------
# Chargeur
# ---------------------------------------------------------------------------

def _map_outside_code(text: str, fn) -> str:
    """Applique fn au texte hors blocs de code ``` / ~~~."""
    segments: list[tuple[bool, list[str]]] = []
    cur: list[str] = []
    in_code, marker = False, ""
    for line in text.split("\n"):
        s = line.lstrip()
        if not in_code and (s.startswith("```") or s.startswith("~~~")):
            segments.append((False, cur))
            cur, in_code, marker = [line], True, s[:3]
        elif in_code and s.startswith(marker):
            cur.append(line)
            segments.append((True, cur))
            cur, in_code = [], False
        else:
            cur.append(line)
    segments.append((in_code, cur))
    return "\n".join(
        "\n".join(lines) if is_code else fn("\n".join(lines))
        for is_code, lines in segments if lines
    )


class MarkdownLoader:
    def __init__(self, flavor: str = "auto", vault_root: Path | None = None,
                 max_display_width: int = 760, fetch_remote: bool = False):
        self.flavor = flavor  # "auto" | "standard" | "obsidian"
        self.vault_root = Path(vault_root) if vault_root else None
        self.max_display_width = max_display_width
        self.fetch_remote = fetch_remote

    # -- API publique ------------------------------------------------------

    def load_file(self, path) -> Document:
        path = Path(path)
        text = path.read_text(encoding="utf-8-sig")
        vault = self.vault_root or find_vault_root(path.resolve())
        return self._load(text, path.parent.resolve(), vault, path)

    def load_text(self, text: str, base_dir=".") -> Document:
        return self._load(text, Path(base_dir).resolve(), self.vault_root, None)

    # -- implémentation ----------------------------------------------------

    def _load(self, text, base_dir, vault, source) -> Document:
        obsidian = self.flavor == "obsidian" or (
            self.flavor == "auto" and bool(OBSIDIAN_HINTS.search(text)))
        res = AssetResolver(base_dir, vault)
        warnings: list[str] = []

        if obsidian:
            text = self._obsidian(text, res, warnings, depth=0)
        text = _map_outside_code(text, lambda s: TASK_RE.sub(
            lambda m: m.group(1) + ("☑ " if m.group(2) in "xX" else "☐ "), s))

        md = make_parser(obsidian)
        tokens = md.parse(text)
        if obsidian:
            self._callouts(tokens, md)
        self._images(tokens, res, warnings)

        meta = {}
        for t in tokens:
            if t.type == "front_matter":
                meta = parse_front_matter(t.content)
                break
        title = meta.get("title") or self._first_heading(tokens) or (source.stem if source else "Document")
        return Document(tokens, meta, warnings, source, title, obsidian, base_dir, vault)

    @staticmethod
    def _first_heading(tokens) -> str:
        for i, t in enumerate(tokens):
            if t.type == "heading_open" and i + 1 < len(tokens):
                return tokens[i + 1].content
        return ""

    # -- Obsidian -> Markdown standard ------------------------------------

    def _obsidian(self, text, res, warnings, depth) -> str:
        def transform(chunk):
            chunk = COMMENT_RE.sub("", chunk)
            while ESC_PIPE_RE.search(chunk):
                chunk = ESC_PIPE_RE.sub(r"\1|", chunk)
            chunk = EMBED_RE.sub(lambda m: self._embed(m, res, warnings, depth), chunk)
            return WIKI_RE.sub(self._wiki, chunk)
        return _map_outside_code(text, transform)

    def _embed(self, m, res, warnings, depth) -> str:
        name, param = m.group(1).strip(), (m.group(2) or "").strip()
        if Path(name).suffix.lower() in IMAGE_EXT:
            size = re.fullmatch(r"(\d+)(?:x\d+)?", param)
            alt = "" if size else param
            title = f' "w={param}"' if size else ""
            return f"![{alt}](<{name}>{title})"
        # transclusion d'une note
        note = res.resolve_note(name)
        if note is None or depth >= 2:
            warnings.append(f"Note intégrée introuvable ou trop profonde : {name}")
            return f"*[note introuvable : {name}]*"
        body = FRONT_RE.sub("", note.read_text(encoding="utf-8-sig"))
        sub = AssetResolver(note.parent, res.vault_root)
        return "\n\n" + self._obsidian(body, sub, warnings, depth + 1) + "\n\n"

    @staticmethod
    def _wiki(m) -> str:
        target = m.group(1).strip()
        label = m.group(3) or (target + (f" › {m.group(2)}" if m.group(2) else ""))
        return f"[{label}](wikilink:{urllib.parse.quote(target)})"

    # -- post-traitement des tokens ---------------------------------------

    @staticmethod
    def _callouts(tokens, md):
        for i, t in enumerate(tokens):
            if not (t.type == "blockquote_open" and i + 2 < len(tokens)
                    and tokens[i + 1].type == "paragraph_open"
                    and tokens[i + 2].type == "inline"):
                continue
            inline = tokens[i + 2]
            m = CALLOUT_RE.match(inline.content)
            if not m:
                continue
            kind, _fold, title, rest = m.groups()
            t.meta["callout"] = {"type": kind.lower(), "title": title.strip() or kind.capitalize()}
            rest = rest.strip()
            inline.content = rest
            inline.children = md.parseInline(rest, {})[0].children if rest else []

    def _images(self, tokens, res, warnings):
        for t in tokens:
            if t.type == "inline" and t.children:
                for child in t.children:
                    if child.type == "image":
                        self._image(child, res, warnings)

    def _image(self, tok: Token, res: AssetResolver, warnings):
        src = urllib.parse.unquote(tok.attrGet("src") or "")
        w = h = None

        m = re.search(r"\|(\d+)(?:x(\d+))?\s*$", tok.content or "")  # ![alt|300](x.png)
        if m:
            w, h = int(m.group(1)), int(m.group(2)) if m.group(2) else None
            tok.content = tok.content[:m.start()].rstrip()
            if tok.children and tok.children[-1].type == "text":
                tok.children[-1].content = re.sub(
                    r"\|\d+(?:x\d+)?\s*$", "", tok.children[-1].content).rstrip()
        tm = re.fullmatch(r"w=(\d+)(?:x(\d+))?", str(tok.attrGet("title") or ""))  # ![[x.png|300]]
        if tm:
            w, h = int(tm.group(1)), int(tm.group(2)) if tm.group(2) else None
            tok.attrs.pop("title", None)

        if re.match(r"https?://", src):
            path = self._fetch(src, warnings) if self.fetch_remote else None
            if path is None and not self.fetch_remote:
                warnings.append(f"Image distante ignorée (fetch_remote=False) : {src}")
        else:
            path = res.resolve(src)

        try:
            nat_w, nat_h = image_size(path) if path else (0, 0)
        except Exception as exc:  # image corrompue
            warnings.append(f"Image illisible : {src} ({exc})")
            path = None

        if path is None:
            if not re.match(r"https?://", src):
                warnings.append(f"Image introuvable : {src}")
            tok.type, tok.content, tok.children = "text", f"[image introuvable : {src}]", None
            return

        shown = min(w or nat_w, self.max_display_width)
        tok.meta.update(path=str(path), width_px=w or nat_w, height_px=h or nat_h)
        tok.attrSet("src", path.as_uri())
        tok.attrSet("width", str(shown))

    @staticmethod
    def _fetch(url, warnings) -> Path | None:
        cache = Path(tempfile.gettempdir()) / "mdviewer_cache"
        cache.mkdir(exist_ok=True)
        ext = Path(urllib.parse.urlparse(url).path).suffix.lower() or ".png"
        dest = cache / (hashlib.sha1(url.encode()).hexdigest() + ext)
        if not dest.exists():
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "mdviewer"})
                with urllib.request.urlopen(req, timeout=10) as r:
                    dest.write_bytes(r.read())
            except Exception as exc:
                warnings.append(f"Téléchargement impossible : {url} ({exc})")
                return None
        return dest
