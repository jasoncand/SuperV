#!/usr/bin/python3
"""
SuperV — a Windows-11-style clipboard manager for Linux Mint (Cinnamon).

Run as a background daemon. Press Super+V (bind in Cinnamon keyboard
settings) to toggle the history popup.

  • Click an entry or press Enter — copies it back AND auto-pastes.
  • Right-click an entry for: Pin / Unpin, Edit, Delete, Clear all.
  • Pinned items stay forever; normal history is cleared on startup.

Data lives in ~/.local/share/superv/history.json
"""

__version__ = "0.0.1"

import base64
import hashlib
import html
import json
import fcntl
import math
import os
import re
import subprocess
import sys
import threading
import time
from urllib.parse import unquote, urlparse, quote
from html.parser import HTMLParser

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk, Pango

DATA_DIR = os.path.expanduser("~/.local/share/superv")
_LEGACY_DATA_DIR = os.path.expanduser("~/.local/share/clipvault")


def _migrate_data_dir():
    """Move pre-rebrand data (clipvault) to the new superv directory."""
    if os.path.isdir(_LEGACY_DATA_DIR) and not os.path.exists(DATA_DIR):
        try:
            os.makedirs(os.path.dirname(DATA_DIR), exist_ok=True)
            os.rename(_LEGACY_DATA_DIR, DATA_DIR)
        except OSError:
            pass


_migrate_data_dir()

HISTORY_FILE = os.path.join(DATA_DIR, "history.json")
IMAGE_DIR = os.path.join(DATA_DIR, "images")
FOLDERS_FILE = os.path.join(DATA_DIR, "folders.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
# Maximum number of UNPINNED clipboard entries to keep. 0 = unlimited.
# Override with the SUPERV_MAX_ITEMS environment variable (e.g. 500).
# Pinned items are never trimmed regardless of this value.
# Default is unlimited so users don't lose history silently.
try:
    MAX_ITEMS = int(os.environ.get("SUPERV_MAX_ITEMS", "0"))
except ValueError:
    MAX_ITEMS = 0
if MAX_ITEMS < 0:
    MAX_ITEMS = 0


# ---------------------------------------------------------------- entries --
def entry_kind(e):
    return e.get("kind", "text")


def entry_key(e):
    """Stable identity used for de-duplication."""
    k = entry_kind(e)
    if k == "image":
        return "img:" + e.get("file", "")
    if k == "files":
        return "files:" + "\n".join(e.get("uris", []))
    return "text:" + e.get("text", "")


def uri_basename(uri):
    try:
        return os.path.basename(unquote(urlparse(uri).path)) or uri
    except Exception:
        return uri


def entry_searchable(e):
    parts = []
    if e.get("name"):
        parts.append(e["name"])
    if e.get("cmd"):
        parts.append(e["cmd"])
    k = entry_kind(e)
    if k == "files":
        parts.append(" ".join(uri_basename(u) for u in e.get("uris", [])))
    elif k == "image":
        parts.append("[image]")
    else:
        parts.append(e.get("text", ""))
    return " ".join(parts)


# ---------------------------------------------------------- short commands --
# Text-expander triggers: /terms, @terms, #terms, $terms, &terms, *terms,
# !terms — typed anywhere, they offer to expand into the assigned content.
CMD_PREFIXES = "/@#$&*!"
_CMD_RE = re.compile(r"^[/@#$&*!][A-Za-z0-9_-]{1,31}$")


def normalize_cmd(s):
    """Validate a short command. Returns the normalized string or None."""
    if not s:
        return None
    s = s.strip()
    return s if _CMD_RE.match(s) else None


def _tag_markup(name, mode):
    """(open, close) markup for a rich-text buffer tag name."""
    if name == "bold":
        return ("<b>", "</b>")
    if name == "italic":
        return ("<i>", "</i>")
    if name == "underline":
        return ("<u>", "</u>")
    if name.startswith("fg-"):
        c = name[3:]
        if mode == "pango":
            return (f'<span foreground="#{c}">', "</span>")
        return (f'<span style="color:#{c}">', "</span>")
    if name.startswith("bg-"):
        c = name[3:]
        if mode == "pango":
            return (f'<span background="#{c}">', "</span>")
        return (f'<span style="background-color:#{c}">', "</span>")
    if name in ("h1", "h2", "h3"):
        if mode == "pango":
            size = {"h1": "xx-large", "h2": "x-large", "h3": "large"}[name]
            return (f'<span size="{size}" weight="bold">', "</span>")
        return (f"<{name}>", f"</{name}>")
    return ("", "")


def _css_color(val):
    """CSS color string → 'rrggbb', or None."""
    rgba = Gdk.RGBA()
    if val and rgba.parse(val.strip()):
        return "{:02x}{:02x}{:02x}".format(
            int(rgba.red * 255), int(rgba.green * 255),
            int(rgba.blue * 255))
    return None


class _HTMLRuns(HTMLParser):
    """Parse HTML into a list of runs for inserting into a rich
    Gtk.TextBuffer: ("text", str, style) / ("img", pixbuf) /
    ("nl",) / ("cell",) / ("hr",)."""

    BLOCK = {"p", "div", "tr", "table", "ul", "ol", "blockquote",
             "h1", "h2", "h3", "h4", "h5", "h6", "pre", "section",
             "article", "li"}
    VOID = {"br", "img", "hr", "meta", "link", "input", "area",
            "source", "col"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.runs = []
        self.styles = [{}]
        self.ol = []          # ordered-list counters
        self.started = False

    # -- helpers --
    def _style(self):
        return self.styles[-1]

    def _apply_css(self, st, css):
        css = (css or "").lower()
        m = re.search(r"(?:^|[;\s])color\s*:\s*([^;!]+)", css)
        if m:
            c = _css_color(m.group(1))
            if c:
                st["fg"] = c
        m = re.search(r"(?:^|;)\s*(?:background|background-color)\s*:"
                      r"\s*([^;!]+)", css)
        if m:
            c = _css_color(m.group(1))
            if c:
                st["bg"] = c
        if "italic" in css or "oblique" in css:
            st["italic"] = True
        if "underline" in css:
            st["underline"] = True
        m = re.search(r"font-weight\s*:\s*([0-9]+|bold)", css)
        if m and (m.group(1) == "bold" or
                  (m.group(1).isdigit() and int(m.group(1)) >= 550)):
            st["bold"] = True

    def _img(self, a):
        src = a.get("src", "")
        pb = None
        m = re.match(r"data:image/[a-z+]+;base64,(.+)", src, re.S)
        if m:
            try:
                loader = GdkPixbuf.PixbufLoader.new()
                loader.write(base64.b64decode(m.group(1).strip()))
                loader.close()
                pb = loader.get_pixbuf()
            except Exception:
                pb = None
        elif src.startswith("file://"):
            try:
                pb = GdkPixbuf.Pixbuf.new_from_file(
                    unquote(urlparse(src).path))
            except Exception:
                pb = None
        self.runs.append(("img", pb) if pb is not None else
                         ("text", "[image]", {}))

    # -- parser callbacks --
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        st = dict(self._style())
        if tag in ("b", "strong"):
            st["bold"] = True
        elif tag in ("i", "em", "cite", "var"):
            st["italic"] = True
        elif tag in ("u", "ins"):
            st["underline"] = True
        elif tag in ("h1", "h2", "h3"):
            st["head"] = tag
        elif tag == "font":
            c = _css_color(a.get("color", ""))
            if c:
                st["fg"] = c
        self._apply_css(st, a.get("style", ""))
        self.styles.append(st)

        if tag == "hr":
            self.runs.append(("hr",))
        elif tag == "br":
            self.runs.append(("nl",))
        elif tag == "img":
            self._img(a)
        elif tag == "td" or tag == "th":
            self.runs.append(("cell",))
        elif tag == "li":
            if not self.started or self.runs:
                self.runs.append(("nl",))
            if self.ol:
                self.ol[-1] += 1
                self.runs.append(("text", f"{self.ol[-1]}. ", {}))
            else:
                self.runs.append(("text", "\u2022 ", {}))
        elif tag in self.BLOCK and (self.runs or self.started):
            self.runs.append(("nl",))
        if tag == "ol":
            self.ol.append(0)
        self.started = True

    def handle_endtag(self, tag):
        if tag == "ol" and self.ol:
            self.ol.pop()
        if tag == "tr" and self.runs:
            self.runs.append(("cell",))   # closing pipe for the row
        if tag in self.BLOCK and self.runs:
            self.runs.append(("nl",))
        if tag not in self.VOID and len(self.styles) > 1:
            self.styles.pop()

    def handle_data(self, data):
        if not data:
            return
        self.runs.append(("text", data, dict(self._style())))


def _html_to_runs(html_txt):
    """HTML string → runs list (see _HTMLRuns). Empty list on failure."""
    p = _HTMLRuns()
    try:
        p.feed(html_txt)
        p.close()
    except Exception:
        return []
    # collapse: drop leading/trailing newlines and duplicate ones
    runs = p.runs
    while runs and runs[0][0] == "nl":
        runs.pop(0)
    while runs and runs[-1][0] == "nl":
        runs.pop()
    out = []
    for r in runs:
        if r[0] == "nl" and out and out[-1][0] == "nl":
            continue
        if r[0] == "text" and not r[1]:
            continue
        out.append(r)
    return out


def _pixbuf_png_b64(pb):
    """PNG bytes of a pixbuf, base64-encoded (for data-URI <img>)."""
    try:
        ok, data = pb.save_to_bufferv("png", [], [])
        if ok:
            return base64.b64encode(data).decode()
    except Exception:
        pass
    return None


def serialize_rich(buf, mode="html"):
    """Serialize a rich Gtk.TextBuffer to HTML or Pango markup.
    Supports inline tags, bullet/ordered lists (as text prefixes),
    pipe-tables, hr-tagged lines and embedded pixbufs."""
    n = buf.get_line_count()
    out = []
    table_rows = []

    def flush_table():
        if not table_rows:
            return
        if mode == "html":
            out.append("<table>")
            for row in table_rows:
                if row and all(
                        re.fullmatch(r":?-{2,}:?", c)
                        for c in row if c) and any(c for c in row):
                    continue                     # markdown separator row
                cells = "".join(
                    f"<td>{html.escape(c)}</td>" for c in row)
                out.append(f"<tr>{cells}</tr>")
            out.append("</table>\n")
        else:
            for row in table_rows:
                out.append(" | ".join(row) + "\n")
        table_rows.clear()

    for ln in range(n):
        s = buf.get_iter_at_line(ln)
        e = s.copy()
        e.forward_to_line_end()
        text = buf.get_text(s, e, False)
        stripped = text.strip()
        if stripped.startswith("|") and stripped.endswith("|") \
                and stripped.count("|") >= 2:
            table_rows.append(
                [c.strip() for c in stripped.strip("|").split("|")])
            continue
        flush_table()
        if any(t.get_property("name") == "hr" for t in s.get_tags()):
            out.append("<hr>\n" if mode == "html" else "\u2500" * 24 + "\n")
            continue
        it = s.copy()
        open_stack = []
        prev_set = None
        while True:
            if it.compare(e) >= 0:
                break
            pb = it.get_pixbuf()
            names = sorted(t.get_property("name") for t in it.get_tags()
                           if t.get_property("name"))
            if names != prev_set:
                while open_stack:
                    out.append(_tag_markup(open_stack.pop(), mode)[1])
                for nm in names:
                    out.append(_tag_markup(nm, mode)[0])
                    open_stack.append(nm)
                prev_set = names
            if pb is not None:
                if mode == "html":
                    b64 = _pixbuf_png_b64(pb)
                    out.append(f'<img src="data:image/png;base64,{b64}">'
                               if b64 else "[image]")
                else:
                    out.append("\U0001f5bc [image] ")
            else:
                ch = it.get_char()
                if ch == "\ufffc":
                    out.append("[image]" if mode == "pango" else "")
                elif mode == "html":
                    out.append(html.escape(ch, quote=False))
                else:
                    out.append(GLib.markup_escape_text(ch))
            if not it.forward_char():
                break
        while open_stack:
            out.append(_tag_markup(open_stack.pop(), mode)[1])
        if mode == "pango":
            out.append("\n")
        elif ln < n - 1:
            out.append("<br>\n")
    flush_table()
    return "".join(out)


def entry_preview(e, n=90):
    nm = e.get("name")
    if nm:
        one = " ".join(nm.split())
        return one if len(one) <= n else one[: n - 1] + "…"
    k = entry_kind(e)
    if k == "image":
        return "Image {}×{}".format(e.get("w", "?"), e.get("h", "?"))
    if k == "files":
        names = [uri_basename(u) for u in e.get("uris", [])]
        head = ", ".join(names[:3])
        if len(names) > 3:
            head += f" … (+{len(names) - 3} more)"
        return f"{len(names)} item(s): {head}"
    one = " ".join(e.get("text", "").split())
    return one if len(one) <= n else one[: n - 1] + "…"


# ---------------------------------------------------------------- storage --
def load_history():
    """Load the full archive. The popup only shows pinned + current-session
    items; the Library window shows everything."""
    try:
        with open(HISTORY_FILE) as f:
            raw = json.load(f)
    except Exception:
        return []
    # Migrate old format ([str,...] / [{text,ts},...])
    items = []
    for it in raw:
        if isinstance(it, str):
            it = {"text": it, "ts": time.time(), "pinned": False}
        elif isinstance(it, dict):
            it.setdefault("pinned", False)
            it.setdefault("kind", "text")
        kind = entry_kind(it)
        if kind == "image":
            path = os.path.join(IMAGE_DIR, it.get("file", ""))
            if not os.path.isfile(path):
                continue
        elif not it.get("text") and not it.get("uris"):
            continue
        items.append(it)
    return items


def _valid_uris(items):
    """Only accept things that genuinely look like file URIs / paths."""
    out = []
    for u in items or []:
        if isinstance(u, str) and (u.startswith("file://")
                                   or u.startswith("/")):
            out.append(u)
    return out


IMG_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tiff")


# --------------------------------------------------------- built-in icons --
# Tiny embedded icon set drawn with Cairo, so the UI never depends on the
# system's icon theme or fonts. All glyphs live in a 16×16 grid.
try:
    import cairo as _cairo
except ImportError:
    _cairo = None

_ICON_CACHE = {}


def _glyph(ctx, name):
    """Trace one icon into a 16×16 coordinate space."""
    ctx.set_line_width(1.4)
    ctx.set_line_cap(_cairo.LineCap.ROUND)
    ctx.set_line_join(_cairo.LineJoin.ROUND)

    def line(x1, y1, x2, y2):
        ctx.move_to(x1, y1)
        ctx.line_to(x2, y2)

    if name == "pin":
        # Outline thumbtack: flat round head, narrow neck, sharp point.
        ctx.arc(8, 3.5, 3.2, 0, 2 * 3.14159)
        ctx.stroke()
        # neck
        ctx.move_to(6.5, 5.8)
        ctx.line_to(6.5, 7.0)
        ctx.move_to(9.5, 5.8)
        ctx.line_to(9.5, 7.0)
        ctx.stroke()
        # body tapering to point
        ctx.move_to(4.5, 7.0)
        ctx.line_to(8.0, 14.5)
        ctx.line_to(11.5, 7.0)
        ctx.close_path()
        ctx.stroke()
    elif name == "pin-filled":
        # Solid filled thumbtack.
        ctx.arc(8, 3.5, 3.2, 0, 2 * 3.14159)
        ctx.fill()
        # neck
        ctx.rectangle(6.5, 5.8, 3.0, 1.2)
        ctx.fill()
        # body
        ctx.move_to(4.5, 7.0)
        ctx.line_to(8.0, 14.5)
        ctx.line_to(11.5, 7.0)
        ctx.close_path()
        ctx.fill()
    elif name == "pin-empty":
        # Outlined thumbtack with a diagonal slash (unpinned).
        ctx.arc(8, 3.5, 3.2, 0, 2 * 3.14159)
        ctx.stroke()
        ctx.move_to(6.5, 5.8)
        ctx.line_to(6.5, 7.0)
        ctx.move_to(9.5, 5.8)
        ctx.line_to(9.5, 7.0)
        ctx.stroke()
        ctx.move_to(4.5, 7.0)
        ctx.line_to(8.0, 14.5)
        ctx.line_to(11.5, 7.0)
        ctx.close_path()
        ctx.stroke()
        # slash
        line(2.5, 2.5, 13.5, 13.5)
        ctx.stroke()
    elif name == "check":
        ctx.move_to(2.8, 8.6)
        ctx.line_to(6.6, 12.4)
        ctx.line_to(13.4, 3.6)
        ctx.stroke()
    elif name == "doc":
        ctx.rectangle(4.2, 1.8, 7.6, 12.4)
        ctx.stroke()
        for y in (5.2, 7.6, 10):
            line(6.2, y, 9.8, y)
            ctx.stroke()
    elif name == "folder":
        ctx.move_to(2.2, 13)
        ctx.line_to(2.2, 4.4)
        ctx.line_to(6.2, 4.4)
        ctx.line_to(7.7, 6)
        ctx.line_to(13.8, 6)
        ctx.line_to(13.8, 13)
        ctx.close_path()
        ctx.stroke()
    elif name == "picture":
        ctx.rectangle(2.2, 3, 11.6, 10)
        ctx.stroke()
        ctx.arc(5.6, 6.2, 1.05, 0, 2 * 3.14159)
        ctx.stroke()
        ctx.move_to(3.4, 11.6)
        ctx.line_to(7, 8.2)
        ctx.line_to(9.6, 10.6)
        ctx.line_to(11.4, 8.8)
        ctx.line_to(12.8, 10.2)
        ctx.stroke()
    elif name == "trash":
        line(3.4, 4.6, 12.6, 4.6)
        ctx.stroke()
        ctx.move_to(6.2, 4.4)
        ctx.line_to(6.2, 2.6)
        ctx.line_to(9.8, 2.6)
        ctx.line_to(9.8, 4.4)
        ctx.stroke()
        ctx.move_to(4.6, 4.8)
        ctx.line_to(5.2, 13.6)
        ctx.line_to(10.8, 13.6)
        ctx.line_to(11.4, 4.8)
        ctx.close_path()
        ctx.stroke()
        line(6.9, 6.8, 6.9, 11.6)
        ctx.stroke()
        line(9.1, 6.8, 9.1, 11.6)
        ctx.stroke()
    elif name == "pencil":
        ctx.move_to(3, 13.2)
        ctx.line_to(3.7, 10.4)
        ctx.line_to(10.6, 3.5)
        ctx.line_to(12.7, 5.6)
        ctx.line_to(5.8, 12.5)
        ctx.close_path()
        ctx.stroke()
        line(3, 13.2, 4.9, 11.3)
        ctx.stroke()
    elif name == "paste":
        ctx.rectangle(3.4, 3.2, 9.2, 11)
        ctx.stroke()
        ctx.rectangle(5.9, 1.6, 4.2, 3)
        ctx.stroke()
        for y in (7.4, 10):
            line(5.6, y, 10.4, y)
            ctx.stroke()
    elif name == "eraser":
        ctx.move_to(2.6, 11.2)
        ctx.line_to(8.8, 5)
        ctx.line_to(12.4, 8.6)
        ctx.line_to(6.2, 14.8)
        ctx.close_path()
        ctx.stroke()
        line(2, 14.6, 14, 14.6)
        ctx.stroke()
    elif name == "search":
        ctx.arc(6.8, 6.8, 3.9, 0, 2 * 3.14159)
        ctx.stroke()
        line(9.8, 9.8, 13.4, 13.4)
        ctx.stroke()
    elif name == "grid":
        ctx.rectangle(2.5, 2.5, 4.6, 4.6)
        ctx.stroke()
        ctx.rectangle(8.9, 2.5, 4.6, 4.6)
        ctx.stroke()
        ctx.rectangle(2.5, 8.9, 4.6, 4.6)
        ctx.stroke()
        ctx.rectangle(8.9, 8.9, 4.6, 4.6)
        ctx.stroke()
    elif name == "list":
        for y in (3.8, 8.0, 12.2):
            line(2.6, y, 13.4, y)
            ctx.stroke()
    elif name == "tag":
        ctx.move_to(2.5, 2.5)
        ctx.line_to(8.3, 2.5)
        ctx.line_to(13.5, 7.7)
        ctx.line_to(7.7, 13.5)
        ctx.line_to(2.5, 8.3)
        ctx.close_path()
        ctx.stroke()
        ctx.arc(5.4, 5.4, 1.0, 0, 2 * 3.14159)
        ctx.stroke()
    elif name == "gear":
        ctx.arc(8, 8, 3.0, 0, 2 * 3.14159)
        ctx.stroke()
        for i in range(8):
            a = i * math.pi / 4
            line(8 + 4.1 * math.cos(a), 8 + 4.1 * math.sin(a),
                 8 + 5.9 * math.cos(a), 8 + 5.9 * math.sin(a))
            ctx.stroke()
    elif name == "plus":
        line(8, 3.5, 8, 12.5)
        ctx.stroke()
        line(3.5, 8, 12.5, 8)
        ctx.stroke()
    elif name == "zap":
        ctx.move_to(9.5, 1.5)
        ctx.line_to(4.5, 9)
        ctx.line_to(7.5, 9)
        ctx.line_to(6.5, 14.5)
        ctx.line_to(11.5, 7)
        ctx.line_to(8.5, 7)
        ctx.close_path()
        ctx.stroke()
    elif name == "min":
        # single horizontal line (underscore)
        line(3, 12, 13, 12)
        ctx.stroke()
    elif name == "max":
        # square frame
        ctx.rectangle(3, 3, 10, 10)
        ctx.stroke()
    elif name == "close":
        # diagonal cross
        line(3.5, 3.5, 12.5, 12.5)
        ctx.stroke()
        line(12.5, 3.5, 3.5, 12.5)
        ctx.stroke()


def icon_pixbuf(name, px=16, color=None):
    """Render a built-in icon as a GdkPixbuf (cached)."""
    color = color or (0.87, 0.87, 0.88)
    key = (name, px, tuple(color))
    if key in _ICON_CACHE:
        return _ICON_CACHE[key]
    if _cairo is None:
        return None
    ss = max(2, px // 8 * 2)          # supersample factor
    w = h = px * ss
    surf = _cairo.ImageSurface(_cairo.FORMAT_ARGB32, w, h)
    ctx = _cairo.Context(surf)
    ctx.scale(w / 16.0, h / 16.0)
    ctx.set_source_rgba(color[0], color[1], color[2], 1.0)
    _glyph(ctx, name)
    surf.flush()
    pb = Gdk.pixbuf_get_from_surface(surf, 0, 0, w, h)
    pb = pb.scale_simple(px, px, GdkPixbuf.InterpType.BILINEAR)
    _ICON_CACHE[key] = pb
    return pb


def make_icon(name_or_names, px=16, color=None):
    """Gtk.Image from the embedded icon set (name str or list of names)."""
    name = name_or_names[0] if isinstance(name_or_names, (list, tuple)) \
        else name_or_names
    img = Gtk.Image()
    pb = icon_pixbuf(name, px, color)
    if pb is not None:
        img.set_from_pixbuf(pb)
    return img


# ------------------------------------------------------------ app icon --
_APP_ICON_B64 = "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"

def app_icon_pixbuf():
    """The SuperV app icon (embedded PNG) as a GdkPixbuf, or None."""
    if not _APP_ICON_B64:
        return None
    try:
        loader = GdkPixbuf.PixbufLoader.new()
        loader.write(base64.b64decode(_APP_ICON_B64))
        loader.close()
        return loader.get_pixbuf()
    except Exception:
        return None


def app_icon_image(px=20):
    """Gtk.Image showing the app icon at the requested size."""
    pb = app_icon_pixbuf()
    if pb is not None:
        return Gtk.Image.new_from_pixbuf(
            pb.scale_simple(px, px, GdkPixbuf.InterpType.BILINEAR))
    return make_icon("paste", px)


def _looks_like_image_ref(text, html):
    """True when the text/html side of a copy is just an image reference
    (e.g. browser 'Copy image' provides the URL + <img> tag alongside the
    pixels). In that case the image itself is what the user copied."""
    if html and re.search(r"<img\b", html[:500], re.I):
        return True
    t = (text or "").strip()
    if t and "\n" not in t and len(t) < 300:
        t = re.sub(r"^file://", "", t)
        if re.search(r"\.(png|jpe?g|gif|bmp|webp)([?#].*)?$", t, re.I):
            return True
    return False


# ------------------------------------------------- raw clipboard ownership --
# PyGI on Debian/Ubuntu does not expose gtk_clipboard_set_with_data(), which
# we need to offer several clipboard targets (uri-list, text/html, …).
# Bind the C API directly with ctypes.
try:
    _ctypes = __import__("ctypes")
    _gtk3 = _ctypes.CDLL("libgtk-3.so.0")
    _gdk3 = _ctypes.CDLL("libgdk-3.so.0")
    _gtk3.gtk_clipboard_set_with_data.restype = _ctypes.c_int
    _gtk3.gtk_clipboard_set_with_data.argtypes = [_ctypes.c_void_p] * 6
    _gtk3.gtk_selection_data_set.restype = None
    _gtk3.gtk_selection_data_set.argtypes = [
        _ctypes.c_void_p, _ctypes.c_void_p, _ctypes.c_int,
        _ctypes.c_void_p, _ctypes.c_int]
    _gdk3.gdk_atom_intern.restype = _ctypes.c_void_p
    _gdk3.gdk_atom_intern.argtypes = [_ctypes.c_char_p, _ctypes.c_int]
except (OSError, ImportError):
    _gtk3 = None

_CLIP_PAYLOAD = {}


class _GtkTargetEntry(_ctypes.Structure if _gtk3 else object):
    pass


if _gtk3:
    class _GtkTargetEntry(_ctypes.Structure):
        _fields_ = [("target", _ctypes.c_char_p),
                    ("flags", _ctypes.c_uint),
                    ("info", _ctypes.c_uint)]

    _GET_CB = _ctypes.CFUNCTYPE(
        None, _ctypes.c_void_p, _ctypes.c_void_p,
        _ctypes.c_uint, _ctypes.c_void_p)
    _CLEAR_CB = _ctypes.CFUNCTYPE(None, _ctypes.c_void_p, _ctypes.c_void_p)

    def _clip_get(_clipboard, sel_data, info, _ud):
        item = _CLIP_PAYLOAD.get(info)
        if item is None:
            return
        name, data = item
        try:
            atom = _gdk3.gdk_atom_intern(name.encode(), False)
            _gtk3.gtk_selection_data_set(sel_data, atom, 8, data, len(data))
        except Exception:
            pass

    def _clip_clear(_cb, _ud):
        pass

    # Keep callbacks referenced forever — GTK stores raw pointers to them.
    _CLIP_GET_FN = _GET_CB(_clip_get)
    _CLIP_CLEAR_FN = _CLEAR_CB(_clip_clear)


def clipboard_set_payload(clipboard, payload):
    """Own the clipboard serving multiple targets.

    payload: [(target_name: str, data: str|bytes), …]
    Returns True on success.
    """
    if _gtk3 is None or not payload:
        return False
    try:
        arr = (_GtkTargetEntry * len(payload))()
        _CLIP_PAYLOAD.clear()
        for i, (name, data) in enumerate(payload):
            arr[i].target = name.encode()
            arr[i].flags = 0
            arr[i].info = i
            _CLIP_PAYLOAD[i] = (
                name, data if isinstance(data, bytes) else data.encode())
        ok = _gtk3.gtk_clipboard_set_with_data(
            hash(clipboard), _ctypes.cast(arr, _ctypes.c_void_p),
            len(payload), _CLIP_GET_FN, _CLIP_CLEAR_FN, None)
        return bool(ok)
    except Exception:
        return False


def _trim_to_max(history):
    """Trim history to MAX_ITEMS unpinned entries, newest-first.
    Pinned items are never trimmed regardless of MAX_ITEMS."""
    if MAX_ITEMS <= 0 or len(history) <= MAX_ITEMS:
        return list(history)
    pinned = [h for h in history if h.get("pinned")]
    unpinned = [h for h in history if not h.get("pinned")]
    unpinned = unpinned[:MAX_ITEMS]
    # Recombine in original order (pinned at their original positions).
    out, pi, ui = [], 0, 0
    for h in history:
        if h.get("pinned"):
            if pi < len(pinned):
                out.append(pinned[pi]); pi += 1
        else:
            if ui < len(unpinned):
                out.append(unpinned[ui]); ui += 1
    return out


def save_history(history):
    os.makedirs(DATA_DIR, exist_ok=True)
    kept = _trim_to_max(history)
    tmp = HISTORY_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(kept, f)
    os.replace(tmp, HISTORY_FILE)
    _gc_images(kept)


def _gc_images(kept):
    """Delete image files no longer referenced by any entry."""
    try:
        referenced = {e["file"] for e in kept if entry_kind(e) == "image"}
        for e in kept:
            referenced.update(e.get("attach") or [])
        for name in os.listdir(IMAGE_DIR):
            if name.endswith(".png") and name not in referenced:
                os.remove(os.path.join(IMAGE_DIR, name))
    except OSError:
        pass


def save_image_pixbuf(pixbuf):
    """Store a captured image; returns (filename, width, height)."""
    os.makedirs(IMAGE_DIR, exist_ok=True)
    ok, buf = pixbuf.save_to_bufferv("png", [], [])
    if not ok:
        raise RuntimeError("could not encode image")
    name = hashlib.sha1(buf).hexdigest() + ".png"
    path = os.path.join(IMAGE_DIR, name)
    if not os.path.exists(path):
        with open(path, "wb") as f:
            f.write(buf)
    return name, pixbuf.get_width(), pixbuf.get_height()


def load_folders():
    try:
        with open(FOLDERS_FILE) as f:
            data = json.load(f)
        if isinstance(data, list):
            return [str(x) for x in data]
    except Exception:
        pass
    return []


def save_folders(folders):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = FOLDERS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(folders, f)
    os.replace(tmp, FOLDERS_FILE)


def load_settings():
    """Persistent user settings (popup width, etc)."""
    try:
        with open(SETTINGS_FILE) as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def save_settings(settings):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = SETTINGS_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(settings, f)
        os.replace(tmp, SETTINGS_FILE)
    except Exception:
        pass


# ------------------------------------------------------------------ popup --
class ClipPopup(Gtk.Window):
    def __init__(self, daemon):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.daemon = daemon
        self.rows = []

        self.set_title("SuperV")
        self.set_role("superv-popup")
        self.set_decorated(False)
        # We resize the window ourselves from the grip. Let GTK
        # think the window isn't user-resizable so it doesn't try
        # to add its own resize borders.
        self.set_resizable(False)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        # Keep-above is the default; the pin button in the header
        # lets the user toggle this and the choice persists for the
        # lifetime of the daemon process.
        self._pinned_on_top = False
        self.set_keep_above(True)
        self.set_type_hint(Gdk.WindowTypeHint.DIALOG)
        self.set_accept_focus(True)
        self.set_focus_on_map(True)
        # We place the popup manually in show_popup() based on the
        # current mouse / caret position, so don't ask GTK to centre
        # it for us.
        self.set_position(Gtk.WindowPosition.NONE)

        css = Gtk.CssProvider()
        css.load_from_data(b"""
            /* Quick-toggle popup (borderless, rounded) */
            .clip-window { background: rgba(24,24,28,250);
                           border-radius: 14px; border: 1px solid #565664;
                           box-shadow: 0 14px 42px rgba(0,0,0,0.6); }
            /* Library window: square corners so the body lines up with
               the titlebar, and a flat background with a subtle outline. */
            .superv-manager { background: rgba(24,24,28,250);
                              border-radius: 0;
                              border: 1px solid #34343d; }
            .clip-header { padding: 12px 16px 8px 16px; }
            .clip-header button { background: transparent; color: #d6d6de;
                                  border: none; border-radius: 8px;
                                  padding: 4px; }
            .clip-header button:hover { background: rgba(255,255,255,0.10); }
            /* Highlight the pin button when the popup is pinned so
               the user has a clear visual cue of the state. */
            .clip-header button.pin-btn.pinned { background: rgba(255,200,80,0.18); }
            /* Logo button on the left of the titlebar - show a subtle
               highlight when grab mode is active. */
            .logo-btn { background: transparent; border-radius: 6px;
                        padding: 2px; }
            .logo-btn:hover { background: rgba(255,255,255,0.08); }
            .logo-btn.active { background: rgba(80,160,255,0.25);
                               border: 1px solid rgba(80,160,255,0.6); }
            /* Right-edge resize grip - a thin column-resize handle. */
            .resize-grip { background: rgba(255,255,255,0.04);
                           border-left: 1px solid rgba(255,255,255,0.08); }
            .resize-grip:hover { background: rgba(80,160,255,0.18);
                                 border-left-color: rgba(80,160,255,0.6); }
            .clip-title { font-size: 14px; font-weight: bold;
                          color: #ffffff; }
            .clip-count { font-size: 11px; color: #9a9aa6;
                          background: rgba(255,255,255,0.08);
                          border-radius: 9px; padding: 2px 9px;
                          margin-left: 8px; }
            .clip-search { background: #26262d; color: #f2f2f5;
                           border: 1px solid #3c3c46; border-radius: 10px;
                           padding: 9px 13px; font-size: 14px;
                           margin: 4px 12px 8px 12px; }
            .clip-search:focus { border-color: #4f8cff;
                                 background: #2b2b33; }
            .clip-tabs { margin: 2px 12px 4px 12px; }
            .clip-tabs button { background: transparent; color: #9a9aa6;
                                border: none; border-radius: 8px;
                                padding: 4px 14px; font-size: 12px; }
            .clip-tabs button:hover { background: rgba(255,255,255,0.08); }
            .clip-tabs button:checked { background: #2563c9;
                                        color: #ffffff; }
            .clip-list { background: transparent; }
            .clip-row { padding: 10px 12px; margin: 1px 10px;
                        border-radius: 10px; color: #e6e6ec;
                        background: rgba(255,255,255,0.03); }
            .clip-row:hover { background: rgba(255,255,255,0.09); }
            .clip-multi { background: rgba(79,140,255,0.26); }
            .clip-row:selected { background: #2563c9; color: #ffffff; }
            .clip-row:selected label { color: #ffffff; }
            .clip-label { font-size: 14px; color: #e8e8ee; }
            .clip-time { font-size: 11px; color: #96969f; }
            .clip-row:selected .clip-time { color: #d7e3ff; }
            /* text-expander suggestion bubble */
            .sug-window { background: rgba(28,28,34,252);
                          border-radius: 12px;
                          border: 1px solid #4f8cff;
                          box-shadow: 0 8px 26px rgba(0,0,0,0.55); }
            .sug-row { padding: 7px 12px; margin: 1px 8px;
                       border-radius: 8px; color: #e6e6ec;
                       background: rgba(255,255,255,0.03); }
            .sug-row:hover { background: rgba(255,255,255,0.09); }
            .sug-sel { background: #2563c9; }
            .sug-sel label { color: #ffffff; }
            .sug-cmd { font-weight: bold; font-size: 13px; }
            .sug-hint { font-size: 10px; color: #80808a;
                        padding: 5px 12px 6px 12px;
                        border-top: 1px solid #34343d; }
            .empty { color: #888; padding: 32px; }
            .hint { font-size: 11px; color: #80808a; padding: 8px 16px;
                    border-top: 1px solid #34343d; }
            /* manager (Library) headerbar - explicit styling for the
               close / minimize / maximize buttons so the WM-drawn glyphs
               stay visible against the dark titlebar. */
            .clip-hb { background: #1c1c22; color: #ffffff;
                       border-bottom: 1px solid #34343d;
                       min-height: 38px; padding: 0 6px; }
            .clip-hb .title { font-weight: bold; font-size: 13px;
                              color: #ffffff; }
            .clip-hb .subtitle { font-size: 11px; color: #9a9aa6; }
            .clip-hb button { background: transparent; color: #d6d6de;
                              border: none; border-radius: 8px;
                              padding: 5px; min-width: 28px;
                              min-height: 28px; }
            .clip-hb button:hover { background: rgba(255,255,255,0.10); }
            .clip-hb button:checked { background: #2563c9; }
            /* Close button on the titlebar - red on hover like every
               other dark-themed app. */
            .clip-hb button.close-btn:hover { background: #e64545; }
            .clip-hb button.close-btn:hover image {
                color: #ffffff; }
            /* Make sure image widgets inside headerbar buttons inherit
               the foreground color so the stroke is visible. */
            .clip-hb button image { color: #d6d6de; }
            .clip-hb button:hover image { color: #ffffff; }
            .mgr-list-row { padding: 10px 14px; border-radius: 12px;
                            margin: 2px 6px; color: #d9d9e0;
                            background: rgba(255,255,255,0.04);
                            border: 1px solid rgba(255,255,255,0.06); }
            .mgr-list-row:hover { background: rgba(255,255,255,0.10);
                                  border-color: rgba(255,255,255,0.14); }
            .mgr-list-row:selected { background: rgba(37,99,201,0.35);
                                     border-color: #4f8cff; }
            .mgr-list-row:selected label { color: #ffffff; }
            .mgr-list-row + .mgr-list-row { border-top: none; }
            list, treeview { background: transparent; }
            .mgr-list-bg { background: rgba(24,24,28,1);
                           color: #d9d9e0; }
            .mgr-sidebar { background: rgba(255,255,255,0.03);
                           border-right: 1px solid #34343d;
                           padding-top: 8px; }
            .mgr-folder-row { padding: 9px 14px; border-radius: 8px;
                              margin: 2px 8px; color: #d6d6de; }
            .mgr-folder-row:hover { background: rgba(255,255,255,0.08); }
            .mgr-folder-row:selected { background: #2563c9; color: #fff; }
            .mgr-folder-row:selected label { color: #ffffff; }
            .mgr-toolbar { background: rgba(255,255,255,0.03);
                           border: 1px solid #30303a; border-radius: 10px;
                           padding: 6px; margin: 0 2px; }
            .mgr-toolbar button { background: #26262d; color: #e6e6ec;
                                  border: 1px solid #3c3c46;
                                  border-radius: 8px;
                                  padding: 6px 11px; font-size: 12px; }
            .mgr-toolbar button:hover { background: #33333d; }
            .mgr-toolbar separator { background: #34343d; }
            .mgr-card { background: rgba(255,255,255,0.04);
                        border: 1px solid rgba(255,255,255,0.06);
                        border-radius: 12px; padding: 12px 10px; }
            .mgr-card:hover { background: rgba(255,255,255,0.10);
                              border-color: rgba(255,255,255,0.14); }
            flowboxchild { background: transparent; border-radius: 12px; }
            flowboxchild:selected { background: transparent; }
            flowboxchild:selected .mgr-card {
                background: rgba(37,99,201,0.35);
                border-color: #4f8cff; }
            .mgr-card-label { font-size: 12px; color: #d9d9e0;
                              padding: 2px 4px 0 4px; }
            .mgr-preview { min-height: 84px; }
            .mgr-settings-btn { background: #26262d; color: #e6e6ec;
                                border: 1px solid #3c3c46;
                                border-radius: 8px;
                                padding: 8px 14px; font-size: 13px; }
            .mgr-settings-btn:hover { background: #33333d; }
        """)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), css,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.get_style_context().add_class("clip-window")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(box)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                         spacing=6)
        header.get_style_context().add_class("clip-header")
        # The logo: clicking it toggles "grab mode" — the cursor turns
        # into a grab/grabbing hand and click-drag moves the popup.
        self._grab_mode = False
        self._grab_dragging = False
        self._grab_drag_offset = None
        self._resize_active = False
        self.logo_btn = Gtk.EventBox()
        self.logo_btn.get_style_context().add_class("logo-btn")
        title_icon = app_icon_image(20)
        self.logo_btn.add(title_icon)
        self.logo_btn.set_tooltip_text(
            "Click to grab the popup (cursor changes to a hand). "
            "Click again to release.")
        self.logo_btn.add_events(Gdk.EventMask.BUTTON_PRESS_MASK
                                 | Gdk.EventMask.ENTER_NOTIFY_MASK
                                 | Gdk.EventMask.LEAVE_NOTIFY_MASK)
        self.logo_btn.connect("button-press-event",
                              self._on_logo_press)
        header.pack_start(self.logo_btn, False, False, 0)
        title = Gtk.Label(label="SuperV", xalign=0)
        title.get_style_context().add_class("clip-title")
        header.pack_start(title, False, False, 0)
        self.count_badge = Gtk.Label(label="0")
        self.count_badge.set_valign(Gtk.Align.CENTER)
        self.count_badge.get_style_context().add_class("clip-count")
        header.pack_end(self.count_badge, False, False, 0)
        # Pin-to-screen toggle (always-on-top). Filled icon = pinned.
        self.pin_btn = Gtk.Button()
        self.pin_btn.set_relief(Gtk.ReliefStyle.NONE)
        self.pin_btn.set_tooltip_text(
            "Pinned: always on top. Click to unpin.")
        self.pin_btn.add(make_icon("pin-filled", 16,
                                   (1.0, 0.78, 0.33)))
        self.pin_btn.connect("clicked", lambda _b: self._toggle_pinned())
        self.pin_btn.get_style_context().add_class("pin-btn")
        self.pin_btn.get_style_context().add_class("pinned")
        header.pack_end(self.pin_btn, False, False, 0)
        lib_btn = Gtk.Button()
        lib_btn.set_relief(Gtk.ReliefStyle.NONE)
        lib_btn.set_tooltip_text("Open library (manage all items)")
        lib_btn.add(make_icon("folder", 16))
        lib_btn.connect(
            "clicked",
            lambda _b: (self.dismiss("open-library"),
                        self.daemon.open_manager()))
        header.pack_end(lib_btn, False, False, 0)

        # Drag-to-move is handled at the window level. A press only
        # reaches the window handler when no child (row, entry,
        # button, grip) consumed it, so empty areas — header,
        # hint row, blank list space — act as drag handles while
        # interactive widgets keep normal behaviour. (Per-Box
        # handlers on windowless Gtk.Box never fired for bare
        # header/hint areas.)
        self._drag_offset = None
        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK
                        | Gdk.EventMask.BUTTON_RELEASE_MASK
                        | Gdk.EventMask.POINTER_MOTION_MASK)
        self.connect("button-press-event", self._on_win_press)
        self.connect("button-release-event", self._on_win_release)
        self.connect("motion-notify-event", self._on_win_motion)

        box.pack_start(header, False, False, 0)

        self.search = Gtk.SearchEntry()
        self.search.set_placeholder_text("Search clipboard history…")
        self.search.get_style_context().add_class("clip-search")
        self.search.connect("search-changed", lambda _e: self.rebuild())
        self.search.connect("key-press-event", self.on_search_key)
        box.pack_start(self.search, False, False, 0)

        # All / Recent / Pinned tabs
        self.tab = "all"
        self.tabs = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                            spacing=4)
        self.tabs.get_style_context().add_class("clip-tabs")
        self.tab_all = Gtk.ToggleButton(label="All")
        self.tab_recent = Gtk.ToggleButton(label="Recent")
        self.tab_pinned = Gtk.ToggleButton(label="Pinned")
        self.tab_all.set_active(True)
        self.tab_all.connect("toggled", self._on_tab, "all")
        self.tab_recent.connect("toggled", self._on_tab, "recent")
        self.tab_pinned.connect("toggled", self._on_tab, "pinned")
        self.tabs.pack_start(self.tab_recent, False, False, 0)
        self.tabs.pack_start(self.tab_pinned, False, False, 0)
        self.tabs.pack_start(self.tab_all, False, False, 0)
        box.pack_start(self.tabs, False, False, 0)

        # Load saved width (default = half of the legacy 540 default).
        settings = load_settings()
        try:
            saved_w = int(settings.get("popup_width", 270))
        except (TypeError, ValueError):
            saved_w = 270
        saved_w = max(240, min(saved_w, 900))
        self._popup_width = saved_w
        self._width_save_id = None  # debounce id for saving

        # Lock the window to the saved width (max only, so the window
        # can still grow if the user resizes it via the grip).
        self.set_size_request(saved_w + 8, -1)
        self._min_width = saved_w + 8
        self._max_width = 900

        self.scroller = Gtk.ScrolledWindow()
        self.scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scroller.set_size_request(saved_w, 420)
        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.listbox.get_style_context().add_class("clip-list")
        self.listbox.connect("row-activated", self._on_activate)
        self.listbox.connect("button-press-event", self.on_button_press)
        self.scroller.add(self.listbox)

        # Resize grip on the right edge — drag it horizontally to
        # change the popup width. The grip is a thin column-resize
        # handle next to the scroller.
        self._resize_grip = Gtk.EventBox()
        self._resize_grip.set_size_request(8, -1)
        self._resize_grip.get_style_context().add_class("resize-grip")
        self._resize_grip.set_tooltip_text(
            "Drag to resize the popup width")
        self._resize_grip.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
            | Gdk.EventMask.ENTER_NOTIFY_MASK
            | Gdk.EventMask.LEAVE_NOTIFY_MASK)
        self._resize_grip.connect("button-press-event",
                                  self._on_resize_press)
        self._resize_grip.connect("button-release-event",
                                  self._on_resize_release)
        self._resize_grip.connect("motion-notify-event",
                                  self._on_resize_motion)
        self._resize_grip.connect("enter-notify-event",
                                  self._on_resize_enter)
        self._resize_grip.connect("leave-notify-event",
                                  self._on_resize_leave)
        # Add a visible filler so the grip is always drawn.
        grip_fill = Gtk.Box()
        grip_fill.set_size_request(8, -1)
        self._resize_grip.add(grip_fill)

        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        body.pack_start(self.scroller, True, True, 0)
        body.pack_end(self._resize_grip, False, False, 0)
        box.pack_start(body, True, True, 0)
        self._body = body

        hint = Gtk.Label(
            label="Enter paste · Click outside or Esc to close · Click pin to keep on screen · Click logo to grab · Drag right edge to resize")
        hint.set_xalign(0)
        hint.get_style_context().add_class("hint")
        box.pack_start(hint, False, False, 0)

        self.connect("key-press-event", self.on_key)
        self.connect("focus-out-event", self._on_focus_out)
        self.edit_dialog = None
        self.pointer_grabbed = False
        self._menu_open = False
        self._shown_at = 0.0
        self._focus_misses = 0
        self.multi = set()  # entry keys ticked for multi-paste
        self.rebuild()

    # ---- UI helpers ----
    def filtered(self):
        q = self.search.get_text().lower()
        d = self.daemon
        items = [h for h in d.history if entry_searchable(h).strip()]
        if self.tab == "pinned":
            items = [h for h in items if h.get("pinned")]
        elif self.tab == "recent":
            # Recent tab: pinned items + everything captured this session
            items = [h for h in items
                     if h.get("pinned") or entry_key(h) in d.session_keys]
        # "all": entire saved history
        if q:
            items = [h for h in items if q in entry_searchable(h).lower()]
            cmd_hits = [h for h in items
                        if h.get("cmd") and h["cmd"].lower().startswith(q)]
            skip = {id(h) for h in cmd_hits}
            items = cmd_hits + [h for h in items if id(h) not in skip]
        if q or self.tab == "all":
            return items
        return items[:25]

    def _on_tab(self, btn, tab):
        if not btn.get_active():
            return
        if self.tab != tab:
            self.tab = tab
            self.tab_all.set_active(tab == "all")
            self.tab_recent.set_active(tab == "recent")
            self.tab_pinned.set_active(tab == "pinned")
            self.rebuild()

    @staticmethod
    def age(ts):
        d = max(1, int(time.time() - ts))
        if d < 60:
            return "just now"
        if d < 3600:
            return f"{d // 60}m ago"
        if d < 86400:
            return f"{d // 3600}h ago"
        return f"{d // 86400}d ago"

    def rebuild(self):
        sel_key = None
        sel = self.listbox.get_selected_row()
        if sel is not None and hasattr(sel, "entry"):
            sel_key = entry_key(sel.entry)

        self.listbox.foreach(lambda c: self.listbox.remove(c))
        self.rows = []
        items = self.filtered()

        if not items:
            if self.tab == "pinned":
                msg = "No pinned items\nPin items from the All tab (Ctrl+P)"
            elif self.daemon.history:
                msg = "No matches"
            else:
                msg = ("Clipboard history is empty\n"
                       "Copy something, then press Super+V")
            lbl = Gtk.Label(label=msg)
            lbl.get_style_context().add_class("empty")
            lbl.set_justify(Gtk.Justification.CENTER)
            self.listbox.add(lbl)
            self.listbox.show_all()
            return

        restore_row = None
        for h in items:
            row = Gtk.ListBoxRow()
            row.get_style_context().add_class("clip-row")
            row.entry = h
            hb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

            # leading indicators: multi-select tick + pin
            ind = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=3)
            if entry_key(h) in self.multi:
                ind.pack_start(
                    make_icon("check", 15, (0.42, 0.85, 0.52)),
                    False, False, 0)
                row.get_style_context().add_class("clip-multi")
            if h["pinned"]:
                ind.pack_start(
                    make_icon("pin", 15, (1.0, 0.78, 0.33)),
                    False, False, 0)
            if ind.get_children():
                ind.set_valign(Gtk.Align.CENTER)
                hb.pack_start(ind, False, False, 0)

            thumb = self._thumbnail(h)
            if thumb is not None:
                hb.pack_start(thumb, False, False, 0)
            else:
                k = entry_kind(h)
                glyph = ("picture" if k == "image" else
                         "folder" if k == "files" else "doc")
                type_icon = make_icon(glyph, 28)
                type_icon.set_valign(Gtk.Align.CENTER)
                type_icon.set_margin_start(4)
                hb.pack_start(type_icon, False, False, 0)

            vb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            t = Gtk.Label(xalign=0)
            t.set_ellipsize(3)  # PANGO_ELLIPSIZE_END
            t.set_text(entry_preview(h))
            t.get_style_context().add_class("clip-label")
            s = Gtk.Label(label=self.age(h["ts"]), xalign=0)
            s.get_style_context().add_class("clip-time")
            vb.pack_start(t, False, False, 0)
            vb.pack_start(s, False, False, 0)
            hb.pack_start(vb, True, True, 0)
            row.add(hb)
            self.listbox.add(row)
            self.rows.append(row)
            if sel_key is not None and entry_key(h) == sel_key:
                restore_row = row

        self.listbox.show_all()
        try:
            total = len(self.daemon.history)
            pinned = sum(1 for x in self.daemon.history if x["pinned"])
            self.count_badge.set_text(
                f"{total} items · {pinned} pinned" if pinned
                else f"{total} items")
        except Exception:
            pass
        GLib.idle_add(lambda: (
            self.listbox.select_row(restore_row),
            self._ensure_visible(restore_row), False)[1])

    @staticmethod
    def _thumbnail(entry):
        """Preview image for image entries and single-image file entries."""
        path = None
        try:
            k = entry_kind(entry)
            if k == "image":
                path = os.path.join(IMAGE_DIR, entry.get("file", ""))
            elif k == "files" and len(entry.get("uris", [])) == 1:
                p = unquote(urlparse(entry["uris"][0]).path)
                if p.lower().endswith(IMG_EXTS) and os.path.isfile(p):
                    path = p
            if not path:
                return None
            pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, 160, 96, True)
            img = Gtk.Image.new_from_pixbuf(pb)
            img.set_valign(Gtk.Align.CENTER)
            return img
        except Exception:
            return None

    # ---- selection helpers ----
    def dismiss(self, reason="manual"):
        try:
            self.daemon.log(f"dismiss ({reason})")
        except Exception:
            pass
        self._reset_interaction_state()
        self._release_grab()
        self.hide()

    def _grab_pointer(self, attempt=0):
        if not self.get_visible():
            return False
        win = self.get_window()
        if win is None:
            return False
        if attempt < 6:
            self.present()
            self.search.grab_focus()
            try:
                subprocess.Popen(
                    ["xdotool", "windowfocus", str(win.get_xid())],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass
        display = self.get_display()
        seat = display.get_default_seat()
        cursor = Gdk.Cursor.new_from_name(display, "default")
        status = seat.grab(win, Gdk.SeatCapabilities.ALL,
                           True, cursor, None, None)
        self.pointer_grabbed = status == Gdk.GrabStatus.SUCCESS
        if not self.pointer_grabbed and attempt < 5:
            GLib.timeout_add(120, self._grab_pointer, attempt + 1)
        return False

    @staticmethod
    def _active_window_id():
        try:
            out = subprocess.run(
                ["xdotool", "getactivewindow"],
                capture_output=True, text=True, timeout=1)
            return int(out.stdout.strip())
        except Exception:
            return None

    def _poll_active_window(self):
        """Fallback: hide when focus moves to a different window."""
        if not self.get_visible():
            return False
        if self._menu_open or self.edit_dialog is not None:
            return True  # menu/dialog is up — don't misread focus changes
        # Pinned popups stay put no matter which window has focus.
        if self._pinned_on_top:
            return True
        active = self._active_window_id()
        win = self.get_window()
        me = win.get_xid() if win is not None else None
        if (me is not None and active is not None
                and active != me
                and active != getattr(self, "_prev_active", None)
                and time.time() - self._shown_at > 0.3):
            self._focus_misses += 1
            if self._focus_misses >= 2:
                self.dismiss("poll-focus")
                return False
        else:
            self._focus_misses = 0
        return True

    def _release_grab(self):
        if self.pointer_grabbed:
            try:
                self.get_display().get_default_seat().ungrab()
            except Exception:
                pass
            self.pointer_grabbed = False

    def _ensure_visible(self, row):
        """Scroll the popup list so the given row is in view."""
        if row is None:
            return

        def do_scroll():
            va = self.scroller.get_vadjustment()
            if va is None or va.get_page_size() <= 0:
                return False
            alloc = row.get_allocation()
            top = va.get_value()
            bottom = top + va.get_page_size()
            y, h = alloc.y, alloc.height
            if h > 0 and (y < top or y + h > bottom):
                va.set_value(min(max(0, y - 4),
                                 va.get_upper() - va.get_page_size()))
            return False
        GLib.idle_add(do_scroll)

    def move_selection(self, delta):
        sel = self.listbox.get_selected_row()
        if sel not in self.rows:
            nxt = 0 if delta > 0 else len(self.rows) - 1
        else:
            idx = self.rows.index(sel)
            nxt = min(max(0, idx + delta), len(self.rows) - 1)
        if 0 <= nxt < len(self.rows):
            self.listbox.select_row(self.rows[nxt])
            self._ensure_visible(self.rows[nxt])

    def commit_selected(self):
        if self.multi:
            mset = set(self.multi)
            entries = [h for h in self.daemon.history
                       if entry_key(h) in mset]
        else:
            sel = self.selected_row()
            if sel is None and self.search.get_text().strip() and self.rows:
                # Searching with no explicit selection: Enter pastes top match
                sel = self.rows[0]
            entries = [sel.entry] if sel is not None else []
        if entries:
            self.dismiss()
            self.daemon.commit_multi(entries)
            return True
        return False

    def toggle_multi(self, entry):
        k = entry_key(entry)
        if k in self.multi:
            self.multi.discard(k)
        else:
            self.multi.add(k)
        self.rebuild()

    def _toggle_current(self):
        sel = self.selected_row()
        if sel is not None:
            self.toggle_multi(sel.entry)

    def selected_row(self):
        sel = self.listbox.get_selected_row()
        return sel if hasattr(sel, "entry") else None

    # ---- actions ----
    def toggle_pin(self, entry):
        for h in self.daemon.history:
            if h is entry:
                h["pinned"] = not h["pinned"]
                break
        self.daemon.save()
        self.rebuild()

    def delete_item(self, entry):
        for i, h in enumerate(self.daemon.history):
            if h is entry:
                del self.daemon.history[i]
                break
        self.daemon.save()
        self.rebuild()

    def clear_unpinned(self):
        self.daemon.log("clear-all-unpinned triggered from menu")
        self.daemon.history = [h for h in self.daemon.history if h["pinned"]]
        self.daemon.save()
        self.rebuild()

    def edit_item(self, entry):
        if entry_kind(entry) == "image":
            return
        old_text = entry.get("text", "")
        dlg = Gtk.Dialog(title="Edit clipboard entry", transient_for=self,
                         modal=True)
        self.edit_dialog = dlg
        dlg.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                        Gtk.STOCK_SAVE, Gtk.ResponseType.OK)
        dlg.set_default_size(480, 160)
        buf = Gtk.TextView()
        buf.get_buffer().set_text(old_text)
        buf.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        frame = Gtk.Frame()
        frame.add(buf)
        dlg.get_content_area().set_border_width(10)
        lbl = Gtk.Label(label="Edit the entry, then Save:")
        lbl.set_halign(Gtk.Align.START)
        dlg.get_content_area().pack_start(lbl, False, False, 4)
        dlg.get_content_area().pack_start(frame, True, True, 4)
        self._release_grab()
        dlg.show_all()

        def response(_d, resp):
            if resp == Gtk.ResponseType.OK:
                new_text = buf.get_buffer().get_text(
                    *buf.get_buffer().get_bounds(), include_hidden_chars=False)
                new_text = new_text.strip()
                for h in self.daemon.history:
                    if h is entry:
                        if new_text:
                            h["text"] = new_text
                            h["kind"] = "text"
                            h.pop("html", None)
                            h["ts"] = time.time()
                        else:
                            self.daemon.history.remove(h)
                        break
                self.daemon.save()
                self.daemon.last_key = entry_key(entry) if new_text else None
                self.rebuild()
            self.edit_dialog = None
            dlg.destroy()
            if self.get_visible():
                GLib.idle_add(self._grab_pointer)
        dlg.connect("response", response)

    # ---- events ----
    def on_button_press(self, _lb, event):
        if event.button == 1 and \
                event.state & Gdk.ModifierType.CONTROL_MASK:
            r = self.listbox.get_row_at_y(int(event.y))
            if r is not None and hasattr(r, "entry"):
                self.toggle_multi(r.entry)
                return True  # swallow so it doesn't paste
        if event.button != 3:  # right click only
            return False
        r = self.listbox.get_row_at_y(int(event.y))
        if r is None:
            return False
        self.listbox.select_row(r)
        menu = Gtk.Menu()

        def item(label, icon_name, cb, accel=None):
            mi = Gtk.MenuItem()
            hb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            hb.pack_start(make_icon(icon_name, 16), False, False, 0)
            text = f"{label}  ({accel})" if accel else label
            lbl = Gtk.Label(label=text, xalign=0)
            lbl.set_hexpand(True)
            hb.pack_start(lbl, True, True, 0)
            mi.add(hb)
            mi.connect("activate", lambda _m: cb(r.entry))
            menu.append(mi)

        pinned = r.entry.get("pinned", False)
        item("Unpin" if pinned else "Pin to top", "pin",
             self.toggle_pin, "Ctrl+P")
        if entry_kind(r.entry) != "image":
            item("Edit…", "pencil", self.edit_item)
        item("Delete", "trash", self.delete_item, "Del")
        if self.multi:
            menu.append(Gtk.SeparatorMenuItem())
            n = len(self.multi)
            mset = set(self.multi)

            def _paste_selected(_m):
                entries = [h for h in self.daemon.history
                           if entry_key(h) in mset]
                self.multi = set()
                self.dismiss()
                self.daemon.commit_multi(entries)
            mi = Gtk.MenuItem()
            hb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            hb.pack_start(make_icon("paste", 16), False, False, 0)
            lbl = Gtk.Label(
                label=f"Paste selected ({n})  (Enter)", xalign=0)
            lbl.set_hexpand(True)
            hb.pack_start(lbl, True, True, 0)
            mi.add(hb)
            mi.connect("activate", _paste_selected)
            menu.append(mi)
        menu.append(Gtk.SeparatorMenuItem())
        item("Clear all unpinned", "eraser",
             lambda _t: self.clear_unpinned())
        menu.show_all()
        self._menu_open = True
        # Give the menu its own grab, otherwise it never closes on outside
        # clicks and the popup's auto-close watcher misfires.
        self._release_grab()

        def _menu_closed(_m):
            self._menu_open = False
            if self.get_visible():
                GLib.idle_add(self._grab_pointer)
        menu.connect("selection-done", _menu_closed)
        menu.connect("deactivate", _menu_closed)
        menu.popup_at_pointer(event)
        return True

    def on_search_key(self, _entry, event):
        state = event.state & Gtk.accelerator_get_default_mod_mask()
        if event.keyval == Gdk.KEY_space and \
                state & Gdk.ModifierType.CONTROL_MASK:
            self._toggle_current(); return True
        if event.keyval == Gdk.KEY_Down:
            self.move_selection(1); return True
        if event.keyval == Gdk.KEY_Up:
            self.move_selection(-1); return True
        if event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            return self.commit_selected()
        return False

    def on_key(self, _w, event):
        kv = event.keyval
        state = event.state & Gtk.accelerator_get_default_mod_mask()
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        if kv in (Gdk.KEY_Print, Gdk.KEY_3270_PrintScreen):
            self._launch_screenshot(); return True
        if kv == Gdk.KEY_Escape:
            if self.multi:
                self.multi = set()
                self.rebuild()
                return True
            self.dismiss(); return True
        if kv == Gdk.KEY_Down and not ctrl:
            self.move_selection(1); return True
        if kv == Gdk.KEY_Up and not ctrl:
            self.move_selection(-1); return True
        if kv in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            return self.commit_selected()
        if kv in (Gdk.KEY_Delete, Gdk.KEY_BackSpace) and not ctrl:
            if self.search.has_focus():
                return False  # let the search box edit its text
            sel = self.selected_row()
            if sel:
                self.delete_item(sel.entry); return True
        if ctrl and kv == Gdk.KEY_space:
            self._toggle_current(); return True
        if ctrl and kv == Gdk.KEY_p:
            sel = self.selected_row()
            if sel:
                self.toggle_pin(sel.entry); return True
        if ctrl and kv == Gdk.KEY_e:
            sel = self.selected_row()
            if sel:
                self.edit_item(sel.entry); return True
        return False

    def _launch_screenshot(self):
        # Our seat grab swallows PrtSc before the DE shortcut can see it,
        # so release the grab, hide, then start the screenshot tool ourselves.
        self.dismiss("screenshot")

        def spawn():
            for cmd in (["gnome-screenshot", "-i"],
                        ["flameshot", "gui"],
                        ["spectacle", "-a"],
                        ["scrot", "select"]):
                try:
                    subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
                    return
                except FileNotFoundError:
                    continue

        GLib.timeout_add(150, spawn)
        return False

    def _on_activate(self, _lb, row):
        self.dismiss()
        self.daemon.commit_and_paste(row.entry)

    def _on_focus_out(self, *_a):
        # Focus loss while OUR OWN menu/edit dialog is open is expected
        # (the WM moves focus when the menu grabs) — not a reason to close.
        if self._menu_open or self.edit_dialog is not None:
            return False
        # When pinned, the popup is meant to stay on screen even when
        # the user clicks another window — that's the whole point.
        if self._pinned_on_top:
            return False
        self.dismiss("focus-out")
        return False

    def do_get_preferred_width(self):
        """Force the popup to a fixed width — don't let GTK grow it
        to fit content beyond what the user asked for."""
        w = getattr(self, "_popup_width", 270) + 8
        return w, w

    def show_popup(self):
        self._reset_interaction_state()
        self.multi = set()
        self.tab = "recent"
        self.tab_all.set_active(False)
        self.tab_recent.set_active(True)
        self.tab_pinned.set_active(False)
        self.rebuild()
        # Place the popup near the current mouse cursor (or the
        # bottom of the focused window if we can't read the cursor).
        self._place_near_cursor()
        self.show_all()
        self.present()
        self.search.grab_focus()
        self._shown_at = time.time()
        self._focus_misses = 0
        self._prev_active = self._active_window_id()
        # No seat grab: that blocks input to every other app while
        # the popup is open. We rely on the Gdk event filter for
        # outside-click detection instead.
        GLib.timeout_add(250, self._poll_active_window)

    def _place_near_cursor(self):
        """Position the popup just below-right of the mouse cursor,
        snapping to the active window's caret area when possible.
        Falls back to screen-centre if no display info is available."""
        display = self.get_display()
        seat = display.get_default_seat()
        try:
            screen, x, y = seat.get_pointer().get_position()
        except Exception:
            x, y = None, None
        if x is None:
            try:
                out = subprocess.run(
                    ["xdotool", "getmouselocation", "--shell"],
                    capture_output=True, text=True, timeout=1)
                info = dict(line.split("=", 1)
                            for line in out.stdout.splitlines()
                            if "=" in line)
                x = int(info.get("X", 0))
                y = int(info.get("Y", 0))
            except Exception:
                x, y = None, None
        if x is None or y is None:
            # Last resort: centre of the primary monitor.
            mon = display.get_primary_monitor() or display.get_monitor(0)
            if mon is not None:
                geo = mon.get_geometry()
                self.move(geo.x + (geo.width - 560) // 2,
                          geo.y + (geo.height - 460) // 2)
            return

        # Try to read the active window's geometry so we can drop the
        # popup next to the text area (typically the upper-middle of
        # the focused window, where a blinking caret usually sits).
        caret_x, caret_y = x, y + 24
        try:
            wid = subprocess.run(
                ["xdotool", "getactivewindow"],
                capture_output=True, text=True, timeout=1)
            wid = wid.stdout.strip()
            if wid and wid != "0":
                geo = subprocess.run(
                    ["xdotool", "getwindowgeometry", "--shell", wid],
                    capture_output=True, text=True, timeout=1)
                g = dict(line.split("=", 1)
                         for line in geo.stdout.splitlines()
                         if "=" in line)
                wx, wy = int(g.get("X", 0)), int(g.get("Y", 0))
                ww, wh = int(g.get("WIDTH", 0)), int(g.get("HEIGHT", 0))
                # If the cursor isn't already inside the active window,
                # fall back to the upper-middle of the focused window
                # (where most text-editing cursors sit).
                if not (wx <= x <= wx + ww and wy <= y <= wy + wh):
                    caret_x = wx + ww // 2 - 100
                    caret_y = wy + max(60, wh // 4)
        except Exception:
            pass

        # Use the explicit size_request we set on the scroller, since
        # get_allocation() may not be valid until the window is mapped.
        # Fall back to get_preferred_size for when the request is unset
        # (which happens after the widget has been shown and hidden once).
        try:
            sr_w, sr_h = self.scroller.get_size_request()
            if sr_w <= 0 or sr_h <= 0:
                nat = self.scroller.get_preferred_size()
                sr_w, sr_h = nat.width or 0, nat.height or 0
            pw, ph = sr_w or 540, sr_h or 420
        except Exception:
            pw, ph = 540, 420
        # Add a small margin for the header, tabs and hint row.
        pw = max(pw + 8, 560)
        ph = max(ph + 140, 460)
        mon = display.get_monitor_at_point(caret_x, caret_y) \
            or display.get_primary_monitor() or display.get_monitor(0)
        if mon is not None:
            work = mon.get_workarea()
            px = max(work.x, min(caret_x, work.x + work.width - pw))
            # Drop below the cursor; if it would overflow, flip above.
            py = caret_y + 18
            if py + ph > work.y + work.height:
                py = max(work.y, caret_y - ph - 18)
            self.move(px, py)
        else:
            self.move(caret_x, caret_y + 18)

    # ------------------ pin-to-screen toggle ------------------
    def _toggle_pinned(self):
        self._pinned_on_top = not self._pinned_on_top
        # Replace the icon to reflect the new state.
        for child in self.pin_btn.get_children():
            self.pin_btn.remove(child)
        ctx = self.pin_btn.get_style_context()
        if self._pinned_on_top:
            self.pin_btn.add(make_icon("pin-filled", 16,
                                       (1.0, 0.78, 0.33)))
            self.pin_btn.set_tooltip_text(
                "Pinned: always on top. Click to unpin.")
            ctx.add_class("pinned")
        else:
            self.pin_btn.add(make_icon("pin-empty", 16))
            self.pin_btn.set_tooltip_text(
                "Unpinned: window will hide on focus loss. Click to pin.")
            ctx.remove_class("pinned")
        self.pin_btn.show_all()
        try:
            self.daemon.log(
                f"popup pin toggled -> {self._pinned_on_top}")
        except Exception:
            pass

    # ------------------ logo / grab mode ------------------
    def _on_logo_press(self, _w, event):
        if event.button != 1:
            return False
        # Toggle grab mode. The cursor switches to a grab hand and
        # the next click-drag on the popup moves it. Clicking the
        # logo again exits grab mode.
        self._set_grab_mode(not self._grab_mode)
        return True

    def _set_grab_mode(self, on):
        self._grab_mode = on
        ctx = self.logo_btn.get_style_context()
        if on:
            ctx.add_class("active")
            self._set_popup_cursor("grab")
            self.logo_btn.set_tooltip_text(
                "Grab mode ON. Click and drag the popup to move it. "
                "Click the logo again to release.")
        else:
            ctx.remove_class("active")
            self._grab_dragging = False
            self._grab_drag_offset = None
            self._set_popup_cursor(None)
            self.logo_btn.set_tooltip_text(
                "Click to grab the popup (cursor changes to a hand). "
                "Click again to release.")
        try:
            self.daemon.log(f"popup grab mode -> {on}")
        except Exception:
            pass

    def _set_popup_cursor(self, name):
        """Set a cursor on the popup window. name=None restores default."""
        try:
            win = self.get_window()
            if win is None:
                return
            display = self.get_display()
            if name is None:
                win.set_cursor(None)
            else:
                cur = Gdk.Cursor.new_from_name(display, name)
                if cur is not None:
                    win.set_cursor(cur)
        except Exception:
            pass

    def _reset_interaction_state(self):
        """Clear any half-finished drag/resize so the next open is clean."""
        was_resizing = getattr(self, "_resize_active", False)
        self._resize_active = False
        self._grab_dragging = False
        self._grab_drag_offset = None
        self._drag_offset = None
        # grab mode is an explicit toggle; turn it off on close so the
        # next open doesn't start with a grab hand.
        if getattr(self, "_grab_mode", False):
            try:
                self._set_grab_mode(False)
            except Exception:
                self._grab_mode = False
                self._set_popup_cursor(None)
        else:
            self._set_popup_cursor(None)
        if was_resizing:
            try:
                self._schedule_width_save()
            except Exception:
                pass

    # ------------------ drag-to-move (window-level) ------------------
    def _on_win_press(self, _w, event):
        if event.button != 1:
            return False
        # Only unhandled presses (empty header/hint/list padding) reach
        # the window; rows/entries/buttons/grip already consumed theirs.
        win_x, win_y = self.get_position()
        if self._grab_mode:
            self._grab_dragging = True
            self._grab_drag_offset = (event.x_root - win_x,
                                      event.y_root - win_y)
            self._set_popup_cursor("grabbing")
        else:
            self._drag_offset = (event.x_root - win_x,
                                 event.y_root - win_y)
        return True

    def _on_win_motion(self, _w, event):
        if self._drag_offset is None and not self._grab_dragging:
            return False
        self._move_to(event.x_root, event.y_root)
        return True

    def _on_win_release(self, _w, _event):
        if self._grab_dragging:
            self._grab_dragging = False
            self._grab_drag_offset = None
            self._set_popup_cursor("grab")
            return True
        self._drag_offset = None
        return False

    def _move_to(self, x_root, y_root):
        """Move the popup so the grab point stays under the cursor."""
        off = self._drag_offset if self._drag_offset is not None else self._grab_drag_offset
        if off is None:
            return
        dx, dy = off
        nx, ny = int(x_root - dx), int(y_root - dy)
        display = self.get_display()
        mon = display.get_monitor_at_point(int(x_root), int(y_root)) \
            or display.get_primary_monitor() or display.get_monitor(0)
        if mon is not None:
            work = mon.get_workarea()
            alloc = self.get_allocation()
            pw, ph = alloc.width, alloc.height
            nx = max(work.x, min(nx, work.x + work.width - pw))
            ny = max(work.y, min(ny, work.y + work.height - ph))
        self.move(nx, ny)

    # ------------------ resize-grip handlers ------------------
    def _on_resize_press(self, _w, event):
        if event.button != 1:
            return False
        win_x, _win_y = self.get_position()
        self._resize_active = True
        self._resize_start_x = event.x_root
        self._resize_start_width = self._popup_width
        self._resize_start_win_x = win_x
        self._set_popup_cursor("col-resize")
        # No explicit seat grab: returning True gives GTK's implicit
        # pointer grab, which already routes motion/release to the
        # grip even outside the window. A Gdk seat grab leaks past
        # dismissal and leaves the next open stuck in resize mode.
        return True

    def _on_resize_release(self, _w, _event):
        if not getattr(self, "_resize_active", False):
            return False
        self._resize_active = False
        self._set_popup_cursor("grab" if self._grab_mode else None)
        self._schedule_width_save()
        return True

    def _on_resize_motion(self, _w, event):
        if not getattr(self, "_resize_active", False):
            return False
        delta = int(event.x_root - self._resize_start_x)
        new_w = self._resize_start_width + delta
        display = self.get_display()
        mon = display.get_monitor_at_point(int(event.x_root),
                                           int(event.y_root)) \
            or display.get_primary_monitor() or display.get_monitor(0)
        max_w = 900
        if mon is not None:
            work = mon.get_workarea()
            max_w = min(900, work.x + work.width
                        - self._resize_start_win_x - 16)
        max_w = max(240, max_w)
        new_w = max(240, min(new_w, max_w))
        if new_w == self._popup_width:
            return True
        self._popup_width = new_w
        self.scroller.set_size_request(new_w, 420)
        self.set_size_request(new_w + 8, -1)
        # set_size_request alone doesn't resize an already-mapped
        # non-resizable window; resize() commits immediately and
        # eliminates the lag / "sometimes doesn't resize" symptom.
        h = self.get_allocated_height()
        try:
            self.resize(new_w + 8, h if h > 1 else 420)
        except Exception:
            pass
        return True

    def _on_resize_enter(self, _w, _event):
        if not getattr(self, "_resize_active", False):
            self._set_popup_cursor("col-resize")
        return False

    def _on_resize_leave(self, _w, _event):
        if not getattr(self, "_resize_active", False):
            self._set_popup_cursor("grab" if self._grab_mode else None)
        return False

    def _schedule_width_save(self):
        """Debounce disk writes so a long drag only saves once."""
        if self._width_save_id is not None:
            try:
                GLib.source_remove(self._width_save_id)
            except Exception:
                pass
        self._width_save_id = GLib.timeout_add(
            400, self._save_popup_width)

    def _save_popup_width(self):
        self._width_save_id = None
        s = load_settings()
        if s.get("popup_width") == self._popup_width:
            return False
        s["popup_width"] = self._popup_width
        save_settings(s)
        return False


# ----------------------------------------------------------------- manager --
class ManagerWindow(Gtk.Window):
    """Library window: browse, edit, delete and organise all items."""

    def __init__(self, daemon):
        super().__init__(title="SuperV — Library")
        self.daemon = daemon
        self.set_role("superv-manager")
        self.set_default_size(880, 600)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.get_style_context().add_class("clip-window")
        self.get_style_context().add_class("superv-manager")
        self.current_group = "__all__"

        hb = Gtk.HeaderBar()
        # Don't use the auto close button — it picks up the system
        # theme colour which is invisible on our dark headerbar.
        # We add our own close button (with a guaranteed-visible icon)
        # alongside explicit minimize/maximize buttons.
        hb.set_show_close_button(False)
        hb.get_style_context().add_class("clip-hb")
        # Title on the left so the right side has room for the
        # minimize/maximize/close buttons.
        custom_title = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                               spacing=8)
        title_lbl = Gtk.Label(label=f"Library v{__version__}")
        title_lbl.get_style_context().add_class("title")
        custom_title.pack_start(title_lbl, False, False, 0)
        hb.set_custom_title(custom_title)
        self._hb = hb
        self.set_titlebar(hb)
        self.view = "list"

        # Explicit window-control buttons. pack_end stacks them
        # right-to-left, so to get the visual order
        # [minimize, maximize, close] we pack them in reverse:
        # close, maximize, minimize.
        min_btn = Gtk.Button()
        min_btn.set_image(make_icon("min", 14))
        min_btn.set_tooltip_text("Minimize")
        min_btn.connect("clicked", lambda *_: self.iconify())

        max_btn = Gtk.Button()
        max_btn.set_image(make_icon("max", 14))
        max_btn.set_tooltip_text("Maximize")
        max_btn.connect("clicked", self._toggle_maximize)

        close_btn = Gtk.Button()
        close_btn.set_image(make_icon("close", 14))
        close_btn.set_tooltip_text("Close")
        close_btn.connect("clicked", lambda *_: self.close())
        close_btn.get_style_context().add_class("close-btn")

        hb.pack_end(close_btn)
        hb.pack_end(max_btn)
        hb.pack_end(min_btn)

        self.search_btn = Gtk.Button()
        self.search_btn.set_image(make_icon("search", 15))
        self.search_btn.set_tooltip_text("Search")
        self.search_btn.connect("clicked", self._toggle_search)
        hb.pack_start(self.search_btn)
        add_menu = Gtk.Menu()
        self._menu_item(add_menu, "Text…", "pencil", self._add_text)
        self._menu_item(add_menu, "File / media…", "folder",
                        self._add_files)
        add_menu.show_all()
        self.add_btn = Gtk.MenuButton()
        self.add_btn.set_image(make_icon("plus", 15))
        self.add_btn.set_tooltip_text("Add custom content")
        self.add_btn.set_popup(add_menu)
        hb.pack_start(self.add_btn)
        self.grid_btn = Gtk.ToggleButton()
        self.grid_btn.set_image(make_icon("grid", 14))
        self.grid_btn.set_tooltip_text("Grid view")
        self.grid_btn.set_active(False)
        self.list_btn = Gtk.ToggleButton()
        self.list_btn.set_image(make_icon("list", 14))
        self.list_btn.set_tooltip_text("List view")
        self.list_btn.set_active(True)
        self.grid_btn.connect("toggled", self._on_view_toggled, "grid")
        self.list_btn.connect("toggled", self._on_view_toggled, "list")
        hb.pack_start(self.grid_btn)
        hb.pack_start(self.list_btn)
        self.cmd_btn = Gtk.ToggleButton()
        self.cmd_btn.set_image(make_icon("zap", 14, (1.0, 0.78, 0.33)))
        self.cmd_btn.set_tooltip_text(
            "Show short-command items only")
        self.cmd_btn.set_active(False)
        self.cmd_btn.connect("toggled", self._on_cmd_toggled)
        hb.pack_start(self.cmd_btn)
        self.cmd_only = False

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        outer.set_border_width(12)
        self.add(outer)

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.search = Gtk.SearchEntry()
        self.search.set_placeholder_text("Search all items…")
        self.search.get_style_context().add_class("clip-search")
        self.search.connect("search-changed", lambda _e: self.refresh_items())
        self.search.connect("key-press-event", self._on_search_key)
        top.pack_start(self.search, True, True, 0)
        outer.pack_start(top, False, False, 0)
        self.search.set_visible(False)
        self.top_box = top

        pane = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        pane.set_position(210)
        outer.pack_start(pane, True, True, 0)

        # ---- sidebar ----
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        side.get_style_context().add_class("mgr-sidebar")
        side_scroller = Gtk.ScrolledWindow()
        side_scroller.set_policy(Gtk.PolicyType.NEVER,
                                 Gtk.PolicyType.AUTOMATIC)
        self.folder_list = Gtk.ListBox()
        self.folder_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.folder_list.connect("row-selected", self._on_folder_selected)
        self.folder_list.connect("button-press-event",
                                 self._on_folder_button_press)
        side_scroller.add(self.folder_list)
        side.pack_start(side_scroller, True, True, 0)

        # bottom section: settings (pinned to bottom of sidebar)
        self.util_list = Gtk.ListBox()
        self.util_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.util_list.connect("row-selected", self._on_util_selected)
        urow = Gtk.ListBoxRow()
        urow.group_id = "__settings__"
        uhb = Gtk.Box(spacing=6)
        uhb.pack_start(make_icon("gear", 14), False, False, 0)
        uhb.pack_start(Gtk.Label(label="Settings", xalign=0), True, True, 0)
        urow.add(uhb)
        urow.get_style_context().add_class("mgr-folder-row")
        self.util_list.add(urow)
        side.pack_end(self.util_list, False, False, 0)
        sep = Gtk.Separator()
        side.pack_end(sep, False, False, 4)
        pane.add1(side)

        # ---- main area ----
        main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC,
                            Gtk.PolicyType.AUTOMATIC)
        scroller.set_hexpand(True)
        scroller.set_vexpand(True)
        self.flow = Gtk.FlowBox()
        self.flow.set_valign(Gtk.Align.START)
        self.flow.set_max_children_per_line(4)
        self.flow.set_min_children_per_line(3)
        self.flow.set_column_spacing(10)
        self.flow.set_row_spacing(10)
        self.flow.set_margin_top(4)
        self.flow.set_margin_left(4)
        self.flow.set_margin_end(10)
        self.flow.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.flow.set_activate_on_single_click(False)
        self.flow.connect("child-activated",
                          lambda _f, ch: self._copy(ch.entry))
        self.flow.connect("button-press-event",
                          self._on_flow_button_press)
        scroller.add(self.flow)
        self._grid_scroller = scroller
        main.pack_start(scroller, True, True, 0)

        # list view — ListBox in ScrolledWindow with Viewport centering fix
        self.list_scroller = Gtk.ScrolledWindow()
        self.list_scroller.set_policy(Gtk.PolicyType.NEVER,
                                      Gtk.PolicyType.AUTOMATIC)
        self.list_scroller.set_hexpand(True)
        self.list_scroller.set_vexpand(True)
        self.item_list = Gtk.ListBox()
        self.item_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.item_list.set_hexpand(True)
        self.item_list.get_style_context().add_class("mgr-list-bg")
        self.item_list.connect("row-activated",
                               lambda _l, r: self._copy(r.entry))
        self.item_list.connect("button-press-event",
                               self._on_list_button_press)
        self.list_scroller.add(self.item_list)
        # GtkViewport auto-centers its child when smaller than viewport.
        # Force the ListBox to fill the viewport height so rows start at top.
        vp = self.list_scroller.get_child()
        if isinstance(vp, Gtk.Viewport):
            def _vp_fix(_vp, alloc):
                nat = self.item_list.get_preferred_height()[1]
                self.item_list.set_size_request(-1, max(nat, alloc.height))
            vp.connect("size-allocate", _vp_fix)
        main.pack_start(self.list_scroller, True, True, 0)

        # settings panel (Import / Export)
        self.settings_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                                    spacing=10)
        self.settings_box.set_border_width(6)
        self.settings_box.set_valign(Gtk.Align.START)
        st = Gtk.Label(label="Settings", xalign=0)
        st.get_style_context().add_class("clip-title")
        self.settings_box.pack_start(st, False, False, 0)
        sd = Gtk.Label(
            label="Export saves all items (images included) and folders "
                  "to a file. Import restores them.",
            xalign=0)
        sd.set_line_wrap(True)
        sd.get_style_context().add_class("clip-time")
        self.settings_box.pack_start(sd, False, False, 0)
        exp_btn = Gtk.Button(label="Export…")
        exp_btn.get_style_context().add_class("mgr-settings-btn")
        exp_btn.connect("clicked", lambda _b: self._export_data())
        self.settings_box.pack_start(exp_btn, False, False, 0)
        imp_btn = Gtk.Button(label="Import…")
        imp_btn.get_style_context().add_class("mgr-settings-btn")
        imp_btn.connect("clicked", lambda _b: self._import_data())
        self.settings_box.pack_start(imp_btn, False, False, 0)
        self.settings_box.set_visible(False)
        main.pack_start(self.settings_box, True, True, 0)

        self.status = Gtk.Label(label="", xalign=0)
        self.status.get_style_context().add_class("hint")
        pane.add2(main)

        self._apply_view()
        self.refresh_folders()
        self.refresh_items()

    # ----------------------------------------------------- view modes --
    def _apply_view(self):
        grid = self.view == "grid"
        in_settings = self.current_group == "__settings__"
        self._grid_scroller.set_visible(grid and not in_settings)
        self.list_scroller.set_visible(not grid and not in_settings)
        self.settings_box.set_visible(in_settings)
        self.grid_btn.set_active(grid)
        self.list_btn.set_active(not grid)

    def _on_view_toggled(self, btn, view):
        if not btn.get_active():
            return
        if self.view != view:
            self.view = view
            self._apply_view()
            self.refresh_items()

    def _on_cmd_toggled(self, btn):
        self.cmd_only = btn.get_active()
        self.refresh_items()

    def _toggle_maximize(self, _b=None):
        if self.get_window() and (self.get_window().get_state()
                                  & Gdk.WindowState.MAXIMIZED):
            self.unmaximize()
        else:
            self.maximize()

    def _toggle_search(self, _b):
        vis = not self.search.get_visible()
        self.search.set_visible(vis)
        if vis:
            self.search.grab_focus()
        elif self.search.get_text():
            self.search.set_text("")
            self.refresh_items()

    def _on_search_key(self, _e, event):
        if event.keyval == Gdk.KEY_Escape:
            if self.search.get_text():
                self.search.set_text("")
                self.refresh_items()
            else:
                self.search.set_visible(False)
            return True
        return False

    # -------------------------------------------------- context menus --
    def _menu_item(self, menu, label, glyph, cb, color=None):
        mi = Gtk.MenuItem()
        hb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        hb.pack_start(make_icon(glyph, 14, color), False, False, 0)
        lbl = Gtk.Label(label=label, xalign=0)
        hb.pack_start(lbl, True, True, 0)
        mi.add(hb)
        mi.connect("activate", lambda _m: cb())
        menu.append(mi)

    def _on_flow_button_press(self, _fb, event):
        if event.button != 3:
            return False
        child = self.flow.get_child_at_pos(int(event.x), int(event.y))
        if child is not None:
            self.flow.select_child(child)
        e = child.entry if child is not None else None
        self._open_item_menu(e, event)
        return True

    def _on_list_button_press(self, _lb, event):
        if event.button != 3:
            return False
        r = self.item_list.get_row_at_y(int(event.y))
        if r is not None:
            self.item_list.select_row(r)
        e = r.entry if r is not None else None
        self._open_item_menu(e, event)
        return True

    def _open_item_menu(self, e, event):
        menu = Gtk.Menu()
        if e is not None:
            k = entry_kind(e)
            pinned = e.get("pinned", False)
            self._menu_item(menu, "Copy", "paste", lambda: self._copy(e))
            if k != "image":
                self._menu_item(menu, "Edit…", "pencil",
                                lambda: self._edit(e))
            self._menu_item(menu, "Rename…", "tag",
                            lambda: self._rename_item(e))
            self._menu_item(menu,
                            "Short command…", "zap",
                            lambda: self._set_cmd(e),
                            (1.0, 0.78, 0.33))
            self._menu_item(menu,
                            "Unpin" if pinned else "Pin", "pin",
                            lambda: self._pin(e),
                            (1.0, 0.78, 0.33))
            sub = Gtk.Menu()
            moveto = Gtk.MenuItem()
            mh = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            mh.pack_start(make_icon("folder", 14), False, False, 0)
            mh.pack_start(Gtk.Label(label="Move to", xalign=0),
                          True, True, 0)
            moveto.add(mh)
            moveto.set_submenu(sub)
            for g in self.daemon.folders:
                mi = Gtk.MenuItem(label=g)
                mi.connect("activate",
                           lambda _m, gg=g: self._move(e, gg))
                sub.append(mi)
            sub.append(Gtk.SeparatorMenuItem())
            mi = Gtk.MenuItem(label="(unsorted)")
            mi.connect("activate", lambda _m: self._move(e, None))
            sub.append(mi)
            menu.append(moveto)
            menu.append(Gtk.SeparatorMenuItem())
            self._menu_item(menu, "Delete", "trash",
                            lambda: self._delete(e), (1.0, 0.45, 0.45))
        else:
            self._set_status("Right-click a card for actions.")
        menu.append(Gtk.SeparatorMenuItem())
        self._menu_item(menu, "New folder…", "folder", self._new_folder)
        menu.show_all()
        menu.popup_at_pointer(event)
        return True

    def _on_folder_button_press(self, _lb, event):
        if event.button != 3:
            return False
        r = self.folder_list.get_row_at_y(int(event.y))
        if r is not None:
            self.folder_list.select_row(r)
        gid = r.group_id if r is not None else "__all__"
        menu = Gtk.Menu()
        self._menu_item(menu, "New folder…", "folder", self._new_folder)
        if gid not in ("__all__", None, "__settings__"):
            menu.append(Gtk.SeparatorMenuItem())
            self._menu_item(menu, f"Rename '{gid}'", "pencil",
                            lambda: self._rename_folder(gid))
            self._menu_item(
                menu, f"Delete folder '{gid}' (items kept)", "trash",
                lambda: self._delete_folder(gid), (1.0, 0.45, 0.45))
        menu.show_all()
        menu.popup_at_pointer(event)
        return True

    # ------------------------------------------------ folder helpers --
    def _user_folders(self):
        return list(self.daemon.folders)

    def refresh_folders(self):
        sel = self.current_group
        self.folder_list.foreach(lambda c: self.folder_list.remove(c))
        counts = {}
        for h in self.daemon.history:
            g = h.get("group")
            counts[g] = counts.get(g, 0) + 1
        rows = [("__all__", "All items"), (None, "Unsorted")]
        rows += [(g, g) for g in self._user_folders()]
        for gid, label in rows:
            r = Gtk.ListBoxRow()
            r.group_id = gid
            n = sum(c for gg, c in counts.items() if
                    (gid == "__all__") or
                    (gid is None and gg is None) or
                    (gid == gg))
            hb = Gtk.Box(spacing=6)
            hb.pack_start(make_icon("folder", 14), False, False, 0)
            hb.pack_start(Gtk.Label(label=f"{label} ({n})", xalign=0),
                          True, True, 0)
            r.add(hb)
            r.get_style_context().add_class("mgr-folder-row")
            self.folder_list.add(r)
            if gid == sel:
                self.folder_list.select_row(r)
        self.folder_list.show_all()
        if sel == "__settings__":
            self.util_list.select_row(self.util_list.get_row_at_index(0))

    def _on_util_selected(self, _lb, row):
        if row is None:
            return
        self.folder_list.select_row(None)
        self.current_group = row.group_id
        self._apply_view()

    def _on_folder_selected(self, _lb, row):
        if row is None:
            return
        self.util_list.select_row(None)
        self.current_group = row.group_id
        self._apply_view()
        if row.group_id != "__settings__":
            self.refresh_items()

    def _filtered_entries(self):
        q = self.search.get_text().lower()
        out = []
        for h in self.daemon.history:
            g = h.get("group")
            if self.current_group == "__all__":
                pass
            elif self.current_group is None:
                if g is not None:
                    continue
            elif g != self.current_group:
                continue
            if q and q not in entry_searchable(h).lower():
                continue
            out.append(h)
        if getattr(self, "cmd_only", False):
            out = [h for h in out if h.get("cmd")]
        # pinned items first, newest first within each section
        pinned = [h for h in out if h.get("pinned")]
        rest = [h for h in out if not h.get("pinned")]
        return sorted(pinned, key=lambda h: -h.get("ts", 0)) + \
            sorted(rest, key=lambda h: -h.get("ts", 0))

    # -------------------------------------------------- item widgets --
    def refresh_items(self):
        for c in self.flow.get_children():
            self.flow.remove(c)
        for c in self.item_list.get_children():
            self.item_list.remove(c)
        items = self._filtered_entries()
        for e in items:
            self.flow.add(self._make_card(e))
            self.item_list.add(self._make_list_row(e))
        self.flow.show_all()
        self.item_list.show_all()
        self._apply_view()
        try:
            total = len(self.daemon.history)
            where = {"__all__": "All items"}.get(
                self.current_group,
                "Unsorted" if self.current_group is None
                else self.current_group)
            if getattr(self, "cmd_only", False):
                where += " · short commands"
            self._hb.set_subtitle(f"{total} items · viewing: {where}")
        except Exception:
            pass

    def _make_list_row(self, e):
        row = Gtk.ListBoxRow()
        row.entry = e
        row.get_style_context().add_class("mgr-list-row")
        hb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        hb.set_margin_top(2)
        hb.set_margin_bottom(2)
        hb.set_margin_start(4)
        hb.set_margin_end(4)

        if e.get("pinned"):
            badge = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                            spacing=4)
            badge.set_valign(Gtk.Align.CENTER)
            badge.pack_start(
                make_icon("pin", 13, (1.0, 0.78, 0.33)), False, False, 0)
            bl = Gtk.Label(label="pinned")
            bl.get_style_context().add_class("clip-time")
            badge.pack_start(bl, False, False, 0)
            hb.pack_start(badge, False, False, 0)

        if e.get("cmd"):
            cb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                         spacing=4)
            cb.set_valign(Gtk.Align.CENTER)
            cb.pack_start(
                make_icon("zap", 13, (1.0, 0.78, 0.33)), False, False, 0)
            cl = Gtk.Label(label=e["cmd"])
            cl.get_style_context().add_class("clip-time")
            cb.pack_start(cl, False, False, 0)
            hb.pack_start(cb, False, False, 0)

        if e.get("attach"):
            ab = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                         spacing=4)
            ab.set_valign(Gtk.Align.CENTER)
            ab.pack_start(
                make_icon("picture", 13, (0.55, 0.75, 1.0)), False, False, 0)
            al = Gtk.Label(label=f"×{len(e['attach'])}")
            al.get_style_context().add_class("clip-time")
            ab.pack_start(al, False, False, 0)
            hb.pack_start(ab, False, False, 0)

        k = entry_kind(e)
        path = None
        try:
            if k == "image":
                p = os.path.join(IMAGE_DIR, e.get("file", ""))
                if os.path.isfile(p):
                    path = p
            elif k == "files" and len(e.get("uris", [])) >= 1:
                p = unquote(urlparse(e["uris"][0]).path)
                if p.lower().endswith(IMG_EXTS) and os.path.isfile(p):
                    path = p
        except Exception:
            path = None
        thumb = None
        if path:
            try:
                pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                    path, 80, 48, True)
                thumb = Gtk.Image.new_from_pixbuf(pb)
                thumb.set_valign(Gtk.Align.CENTER)
            except Exception:
                thumb = None
        if thumb is not None:
            hb.pack_start(thumb, False, False, 0)
        else:
            glyph = ("picture" if k == "image" else
                     "folder" if k == "files" else "doc")
            type_icon = make_icon(glyph, 22)
            type_icon.set_valign(Gtk.Align.CENTER)
            hb.pack_start(type_icon, False, False, 0)

        vb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        vb.set_valign(Gtk.Align.CENTER)
        lbl = Gtk.Label(label=entry_preview(e, 120), xalign=0)
        lbl.set_ellipsize(3)
        lbl.set_hexpand(True)
        lbl.get_style_context().add_class("mgr-card-label")
        lbl.set_size_request(-1, 20)
        vb.pack_start(lbl, False, False, 0)

        tm = Gtk.Label(label=self._age(e.get("ts", 0)), xalign=1)
        tm.get_style_context().add_class("clip-time")
        tm.set_hexpand(False)
        tm.set_margin_end(4)
        hb.pack_start(vb, True, True, 0)
        hb.pack_start(tm, False, False, 0)
        row.add(hb)
        return row

    @staticmethod
    def _age(ts):
        d = max(1, int(time.time() - ts))
        if d < 60:
            return "now"
        if d < 3600:
            return f"{d // 60}m"
        if d < 86400:
            return f"{d // 3600}h"
        return f"{d // 86400}d"

    def _make_card(self, e):
        child = Gtk.FlowBoxChild()
        child.entry = e
        child.get_style_context().add_class("mgr-card")
        child.set_size_request(190, -1)
        child.set_halign(Gtk.Align.FILL)
        v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        v.set_valign(Gtk.Align.START)

        if e.get("pinned"):
            badge = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                            spacing=4)
            badge.set_halign(Gtk.Align.CENTER)
            badge.pack_start(
                make_icon("pin", 13, (1.0, 0.78, 0.33)), False, False, 0)
            bl = Gtk.Label(label="pinned")
            bl.get_style_context().add_class("clip-time")
            badge.pack_start(bl, False, False, 0)
            v.pack_start(badge, False, False, 0)

        if e.get("cmd"):
            cb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                         spacing=4)
            cb.set_halign(Gtk.Align.CENTER)
            cb.pack_start(
                make_icon("zap", 13, (1.0, 0.78, 0.33)), False, False, 0)
            cl = Gtk.Label(label=e["cmd"])
            cl.get_style_context().add_class("clip-time")
            cb.pack_start(cl, False, False, 0)
            v.pack_start(cb, False, False, 0)

        if e.get("attach"):
            ab = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                         spacing=4)
            ab.set_halign(Gtk.Align.CENTER)
            ab.pack_start(
                make_icon("picture", 13, (0.55, 0.75, 1.0)), False, False, 0)
            al = Gtk.Label(label=f"×{len(e['attach'])}")
            al.get_style_context().add_class("clip-time")
            ab.pack_start(al, False, False, 0)
            v.pack_start(ab, False, False, 0)

        k = entry_kind(e)
        path = None
        try:
            if k == "image":
                p = os.path.join(IMAGE_DIR, e.get("file", ""))
                if os.path.isfile(p):
                    path = p
            elif k == "files" and len(e.get("uris", [])) >= 1:
                p = unquote(urlparse(e["uris"][0]).path)
                if p.lower().endswith(IMG_EXTS) and os.path.isfile(p):
                    path = p
        except Exception:
            path = None

        if path:
            try:
                pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                    path, 160, 84, True)
                img = Gtk.Image.new_from_pixbuf(pb)
                img.set_halign(Gtk.Align.CENTER)
                v.pack_start(img, False, False, 0)
            except Exception:
                path = None
        if not path:
            glyph = ("picture" if k == "image" else
                     "folder" if k == "files" else "doc")
            icon = make_icon(glyph, 36)
            icon.set_halign(Gtk.Align.CENTER)
            icon.set_valign(Gtk.Align.CENTER)
            holder = Gtk.Box()
            holder.get_style_context().add_class("mgr-preview")
            holder.pack_start(icon, True, True, 0)
            v.pack_start(holder, False, False, 0)

        lbl = Gtk.Label(label=entry_preview(e, 64))
        lbl.set_line_wrap(True)
        lbl.set_lines(2)
        lbl.set_ellipsize(3)
        lbl.set_line_wrap_mode(2)  # CHAR
        lbl.set_justify(Gtk.Justification.CENTER)
        lbl.set_max_width_chars(24)
        lbl.set_valign(Gtk.Align.START)
        lbl.set_size_request(-1, 36)
        lbl.get_style_context().add_class("mgr-card-label")
        v.pack_start(lbl, False, False, 0)
        child.add(v)
        return child

    def _selected_entry(self):
        sel = self.flow.get_selected_children()
        if sel:
            return sel[0].entry
        return None

    def _set_status(self, msg):
        self.status.set_text(msg)

    # ------------------------------------------------------- actions --
    def _copy(self, e):
        if self.daemon.copy_entry(e):
            self._set_status("Copied to clipboard.")
            self.refresh_all()  # pinned/session state may change badges
        else:
            self._set_status("Copy failed.")

    def _edit(self, e):
        if entry_kind(e) == "image":
            self._set_status("Images can't be edited as text.")
            return
        res = self._text_editor_dialog(
            "Edit item", plain=e.get("text", ""),
            html_init=e.get("html"), cmd=e.get("cmd", ""),
            attach=e.get("attach"))
        if res is None:
            return
        for h in self.daemon.history:
            if h is e:
                if not res["plain"] and not res["html"]:
                    self.daemon.history.remove(h)
                else:
                    h["text"] = res["plain"]
                    h["kind"] = "text"
                    if res["html"]:
                        h["html"] = res["html"]
                    else:
                        h.pop("html", None)
                    if res["attach"]:
                        h["attach"] = res["attach"]
                    else:
                        h.pop("attach", None)
                    if res["cmd"]:
                        h["cmd"] = res["cmd"]
                    else:
                        h.pop("cmd", None)
                break
        self.daemon.save()
        self.refresh_all()

    def _rename_item(self, e):
        new = self._prompt_folder_name(
            "Rename item", e.get("name", ""))
        if new is None:
            return
        if new:
            e["name"] = new
        else:
            e.pop("name", None)
        self.daemon.save()
        self._set_status(f"Renamed to '{new}'." if new else "Name cleared.")
        self.refresh_all()

    # --------------------------------------------- add custom content --
    def _text_editor_dialog(self, title, plain="", html_init=None,
                            cmd="", attach=None):
        """Shared rich-text editor (tabs: Plain / Formatted / Preview,
        image attachment, short command). Returns dict(plain, html,
        cmd, attach) or None when cancelled."""
        dlg = Gtk.Dialog(title=title, transient_for=self, modal=True)
        dlg.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                        Gtk.STOCK_SAVE, Gtk.ResponseType.OK)
        dlg.set_default_size(640, 500)
        box = dlg.get_content_area()
        box.set_border_width(10)
        box.set_spacing(6)

        nb = Gtk.Notebook()
        nb.set_vexpand(True)
        box.pack_start(nb, True, True, 0)

        # ---------------------------------------------------- plain tab --
        tv = Gtk.TextView()
        tv.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        tv.get_buffer().set_text(plain)
        tsc = Gtk.ScrolledWindow()
        tsc.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        tsc.set_vexpand(True)
        tsc.add(tv)
        nb.append_page(tsc, Gtk.Label(label="Plain text"))

        # ----------------------------------------------- formatted tab --
        rich = Gtk.TextView()
        rich.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        buf = rich.get_buffer()
        buf.create_tag("bold", weight=Pango.Weight.BOLD)
        buf.create_tag("italic", style=Pango.Style.ITALIC)
        buf.create_tag("underline", underline=Pango.Underline.SINGLE)
        buf.create_tag("h1", scale=1.9, weight=Pango.Weight.BOLD)
        buf.create_tag("h2", scale=1.5, weight=Pango.Weight.BOLD)
        buf.create_tag("h3", scale=1.22, weight=Pango.Weight.BOLD)
        buf.create_tag("hr", foreground="#77777a")
        color_tags = {}

        def _get_color_tag(prefix, hexcol):
            key = prefix + hexcol
            if key not in color_tags:
                prop = "foreground" if prefix == "fg-" else "background"
                color_tags[key] = buf.create_tag(key, **{prop: "#" + hexcol})
            return key

        def _selection():
            b = buf.get_selection_bounds()
            return (b[0], b[1]) if b else None

        rich_sc = Gtk.ScrolledWindow()
        rich_sc.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        rich_sc.set_vexpand(True)
        rich_sc.add(rich)

        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                          spacing=4)

        def tool_label(markup, tip, cb):
            b = Gtk.Button()
            lbl = Gtk.Label()
            lbl.set_markup(markup)
            b.add(lbl)
            b.set_tooltip_text(tip)
            b.get_style_context().add_class("mgr-settings-btn")
            b.connect("clicked", cb)
            toolbar.pack_start(b, False, False, 0)

        def _toggle_tag(name):
            sel = _selection()
            if sel is None:
                return
            s, e = sel
            if any(t.get_property("name") == name for t in s.get_tags()):
                buf.remove_tag_by_name(name, s, e)
            else:
                buf.apply_tag_by_name(name, s, e)

        heading = Gtk.ComboBoxText()
        for t in ("Paragraph", "Heading 1", "Heading 2", "Heading 3"):
            heading.append_text(t)
        heading.set_active(0)
        heading.set_tooltip_text("Heading / paragraph style")

        def _on_heading(_cmb):
            i = heading.get_active()
            sel = _selection()
            if sel is not None:
                l1, l2 = sel[0].get_line(), sel[1].get_line()
            else:
                l1 = l2 = buf.get_iter_at_mark(
                    buf.get_insert()).get_line()
            ls = buf.get_iter_at_line(l1)
            le = buf.get_iter_at_line(l2)
            le.forward_to_line_end()
            for h in ("h1", "h2", "h3"):
                buf.remove_tag_by_name(h, ls, le)
            if i > 0:
                buf.apply_tag_by_name(f"h{i}", ls, le)
        heading.connect("changed", _on_heading)
        toolbar.pack_start(heading, False, False, 0)

        tool_label("<b>B</b>", "Bold", lambda _b: _toggle_tag("bold"))
        tool_label("<i>I</i>", "Italic", lambda _b: _toggle_tag("italic"))
        tool_label("<u>U</u>", "Underline",
                   lambda _b: _toggle_tag("underline"))

        def _rgba_hex(rgba):
            return "{:02x}{:02x}{:02x}".format(
                int(rgba.red * 255), int(rgba.green * 255),
                int(rgba.blue * 255))

        def _apply_color(prefix):
            sel = _selection()
            if sel is None:
                return
            cd = Gtk.ColorChooserDialog(
                title="Text color" if prefix == "fg-"
                      else "Highlight color",
                transient_for=dlg, modal=True)
            resp = cd.run()
            rgba = cd.get_rgba()
            cd.destroy()
            if resp != Gtk.ResponseType.OK:
                return
            buf.apply_tag_by_name(_get_color_tag(prefix, _rgba_hex(rgba)),
                                  sel[0], sel[1])

        tool_label("<span foreground='#ff6b6b'>A</span>", "Text color",
                   lambda _b: _apply_color("fg-"))
        tool_label(
            "<span background='#ffe066' foreground='#222222'>H</span>",
            "Highlight / background color",
            lambda _b: _apply_color("bg-"))

        def _bullet():
            sel = _selection()
            if sel is not None:
                l1, l2 = sel[0].get_line(), sel[1].get_line()
            else:
                it0 = buf.get_iter_at_mark(buf.get_insert())
                l1 = l2 = it0.get_line()
            it = buf.get_iter_at_line(l1)
            end = it.copy()
            end.forward_to_line_end()
            remove = buf.get_text(it, end, False).startswith("\u2022 ")
            for ln in range(l1, l2 + 1):
                it = buf.get_iter_at_line(ln)
                if remove:
                    end = it.copy()
                    end.forward_chars(2)
                    buf.delete(it, end)
                else:
                    buf.insert(it, "\u2022 ")

        def _ordered():
            sel = _selection()
            if sel is not None:
                l1, l2 = sel[0].get_line(), sel[1].get_line()
            else:
                it0 = buf.get_iter_at_mark(buf.get_insert())
                l1 = l2 = it0.get_line()
            it = buf.get_iter_at_line(l1)
            end = it.copy()
            end.forward_to_line_end()
            remove = bool(re.match(r"^\d+\. ", buf.get_text(it, end, False)))
            num = 1
            for ln in range(l1, l2 + 1):
                it = buf.get_iter_at_line(ln)
                if remove:
                    end = buf.get_iter_at_line(ln)
                    end.forward_to_line_end()
                    m = re.match(r"^\d+\. ",
                                 buf.get_text(buf.get_iter_at_line(ln),
                                              end, False))
                    if m:
                        buf.delete(buf.get_iter_at_line(ln),
                                   buf.get_iter_at_line_offset(
                                       ln, m.end()))
                else:
                    buf.insert(it, f"{num}. ")
                    num += 1

        def _hr():
            ins = buf.get_insert()
            start = buf.create_mark(None, buf.get_iter_at_mark(ins), True)
            buf.insert(buf.get_iter_at_mark(ins), "\u2501" * 20 + "\n")
            s2 = buf.get_iter_at_mark(start)
            e2 = buf.get_iter_at_mark(ins)
            e2.backward_char()          # exclude the trailing newline
            buf.apply_tag_by_name("hr", s2, e2)
            buf.delete_mark(start)

        def _table():
            td = Gtk.Dialog(title="Insert table", transient_for=dlg,
                            modal=True)
            td.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                           Gtk.STOCK_OK, Gtk.ResponseType.OK)
            tb = td.get_content_area()
            tb.set_border_width(10)
            tb.set_spacing(6)
            tb.pack_start(Gtk.Label(label="Rows (incl. header):",
                                    xalign=0), False, False, 0)
            rows = Gtk.SpinButton.new_with_range(2, 30, 1)
            rows.set_value(3)
            tb.pack_start(rows, False, False, 0)
            tb.pack_start(Gtk.Label(label="Columns:", xalign=0),
                          False, False, 0)
            cols = Gtk.SpinButton.new_with_range(1, 10, 1)
            cols.set_value(3)
            tb.pack_start(cols, False, False, 0)
            td.show_all()
            if td.run() != Gtk.ResponseType.OK:
                td.destroy()
                return
            nr, nc = int(rows.get_value()), int(cols.get_value())
            td.destroy()
            head = "|" + "|".join(f" Head{i + 1} " for i in range(nc)) + "|"
            sep = "|" + "|".join([" --- "] * nc) + "|"
            body = "".join(
                "|" + "|".join(["      "] * nc) + "|\n"
                for _ in range(nr - 1))
            it = buf.get_iter_at_mark(buf.get_insert())
            if it.get_line_offset() > 0:
                buf.insert(it, "\n")
            buf.insert(it, head + "\n" + sep + "\n" + body)

        def _insert_image():
            fd = Gtk.FileChooserDialog(
                title="Insert image", transient_for=dlg, modal=True,
                action=Gtk.FileChooserAction.OPEN)
            fd.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                           Gtk.STOCK_OPEN, Gtk.ResponseType.OK)
            f = Gtk.FileFilter()
            f.set_name("Images")
            for e in IMG_EXTS:
                f.add_pattern("*" + e)
            fd.add_filter(f)
            if fd.run() != Gtk.ResponseType.OK:
                fd.destroy()
                return
            path = fd.get_filename()
            fd.destroy()
            try:
                pb = GdkPixbuf.Pixbuf.new_from_file(path)
            except Exception:
                return
            maxw = 480
            if pb.get_width() > maxw:
                pb = pb.scale_simple(
                    maxw,
                    max(1, int(pb.get_height() * maxw / pb.get_width())),
                    GdkPixbuf.InterpType.BILINEAR)
            buf.insert_pixbuf(buf.get_iter_at_mark(buf.get_insert()), pb)

        # paste formatted (HTML) clipboard content with formatting kept
        def _on_rich_paste(tv):
            clip = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
            sd = clip.wait_for_contents(
                Gdk.Atom.intern("text/html", False))
            if sd is None:
                return                      # plain source — default paste
            try:
                raw = bytes(sd.get_data()).decode("utf-8",
                                                  errors="replace")
            except Exception:
                return
            runs = _html_to_runs(raw)
            if not runs:
                return
            tv.stop_emission_by_name("paste-clipboard")
            _apply_runs(runs)

        def _apply_runs(runs):
            ins = buf.get_insert()
            buf.begin_user_action()
            for r in runs:
                it = buf.get_iter_at_mark(ins)
                if r[0] == "nl":
                    buf.insert(it, "\n")
                elif r[0] == "cell":
                    buf.insert(it, "|")
                elif r[0] == "hr":
                    start_m = buf.create_mark(None, it, True)
                    buf.insert(it, "\u2501" * 20)
                    buf.apply_tag_by_name(
                        "hr", buf.get_iter_at_mark(start_m),
                        buf.get_iter_at_mark(ins))
                    buf.delete_mark(start_m)
                elif r[0] == "img":
                    buf.insert_pixbuf(it, r[1])
                elif r[0] == "text":
                    off = it.get_offset()
                    buf.insert(it, r[1])
                    s = buf.get_iter_at_offset(off)
                    e = buf.get_iter_at_offset(off + len(r[1]))
                    st = r[2]
                    if st.get("bold"):
                        buf.apply_tag_by_name("bold", s, e)
                    if st.get("italic"):
                        buf.apply_tag_by_name("italic", s, e)
                    if st.get("underline"):
                        buf.apply_tag_by_name("underline", s, e)
                    if st.get("head"):
                        buf.apply_tag_by_name(st["head"], s, e)
                    if st.get("fg"):
                        buf.apply_tag_by_name(
                            _get_color_tag("fg-", st["fg"]), s, e)
                    if st.get("bg"):
                        buf.apply_tag_by_name(
                            _get_color_tag("bg-", st["bg"]), s, e)
            buf.end_user_action()

        rich.connect("paste-clipboard", _on_rich_paste)

        tool_label("\u2022 List", "Toggle bullet list", lambda _b: _bullet())
        tool_label("1. List", "Toggle ordered list", lambda _b: _ordered())
        tool_label("\u2500\u2500", "Horizontal line", lambda _b: _hr())
        tool_label("\u2637 Table", "Insert table", lambda _b: _table())
        tool_label("\U0001f5bc Image", "Insert image into text",
                   lambda _b: _insert_image())

        tool_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        tool_box.pack_start(toolbar, False, False, 2)
        tool_box.pack_start(rich_sc, True, True, 0)
        nb.append_page(tool_box, Gtk.Label(label="Formatted text"))

        # ------------------------------------------------- preview tab --
        prev_lbl = Gtk.Label(xalign=0, yalign=0)
        prev_lbl.set_selectable(True)
        prev_lbl.set_line_wrap(True)
        prev_lbl.set_valign(Gtk.Align.START)
        prev_lbl.set_margin_start(8)
        prev_lbl.set_margin_end(8)
        prev_lbl.set_margin_top(8)
        prev_lbl.set_margin_bottom(8)

        def _update_preview(*_a):
            # keep the Plain text tab in sync: same content without
            # formatting, preserving empty lines and spaces
            rich_txt = buf.get_text(*buf.get_bounds(),
                                    include_hidden_chars=False)
            ptv = tv.get_buffer()
            if ptv.get_text(*ptv.get_bounds(),
                            include_hidden_chars=False) != rich_txt:
                ptv.set_text(rich_txt)
            markup = serialize_rich(buf, "pango") or "<i>Nothing yet</i>"
            try:
                prev_lbl.set_markup(markup)
            except Exception:
                pass
        buf.connect("changed", _update_preview)
        psc = Gtk.ScrolledWindow()
        psc.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        psc.add(prev_lbl)
        nb.append_page(psc, Gtk.Label(label="Preview"))

        # prefill the formatted tab from stored HTML
        if html_init:
            runs = _html_to_runs(html_init)
            if runs:
                _apply_runs(runs)

        # --------------------------------- image attachment (outside) --
        attach_files = list(attach or [])
        ah = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        attach_btn = Gtk.Button(label="Attach image\u2026")
        attach_btn.set_tooltip_text(
            "Attach an image to this item (pasted together with the text)")
        attach_btn.get_style_context().add_class("mgr-settings-btn")
        albl = Gtk.Label(xalign=0)
        albl.get_style_context().add_class("clip-time")
        clear_btn = Gtk.Button(label="\u2715")
        clear_btn.set_tooltip_text("Remove attached image")
        clear_btn.get_style_context().add_class("mgr-settings-btn")
        ah.pack_start(attach_btn, False, False, 0)
        ah.pack_start(albl, True, True, 0)
        ah.pack_start(clear_btn, False, False, 0)
        box.pack_start(ah, False, False, 0)

        def _update_attach_lbl():
            if attach_files:
                albl.set_text(f"\U0001f5bc {len(attach_files)} image(s) "
                              "attached")
                clear_btn.set_sensitive(True)
            else:
                albl.set_text("No image attached")
                clear_btn.set_sensitive(False)
        clear_btn.connect("clicked", lambda _b: (attach_files.clear(),
                                                 _update_attach_lbl()))
        _update_attach_lbl()

        def _on_attach(_b):
            fd = Gtk.FileChooserDialog(
                title="Attach image", transient_for=dlg, modal=True,
                action=Gtk.FileChooserAction.OPEN)
            fd.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                           Gtk.STOCK_OPEN, Gtk.ResponseType.OK)
            f = Gtk.FileFilter()
            f.set_name("Images")
            for ext in IMG_EXTS:
                f.add_pattern("*" + ext)
            fd.add_filter(f)
            if fd.run() != Gtk.ResponseType.OK:
                fd.destroy()
                return
            path = fd.get_filename()
            fd.destroy()
            os.makedirs(IMAGE_DIR, exist_ok=True)
            try:
                pb = GdkPixbuf.Pixbuf.new_from_file(path)
                ok, data = pb.save_to_bufferv("png", [], [])
                if not ok:
                    raise ValueError("encode failed")
                digest = hashlib.sha1(
                    data + os.path.basename(path).encode()).hexdigest()[:16]
                fn = digest + ".png"
                dst = os.path.join(IMAGE_DIR, fn)
                if not os.path.exists(dst):
                    with open(dst, "wb") as fh:
                        fh.write(data)
                if fn not in attach_files:
                    attach_files.append(fn)
                _update_attach_lbl()
            except Exception as ex:
                self._set_status(f"Attach failed: {ex}")
        attach_btn.connect("clicked", _on_attach)

        # --------------------------------------------- short command ----
        cl = Gtk.Label(label="Short command (optional)", xalign=0)
        box.pack_start(cl, False, False, 0)
        ce = Gtk.Entry()
        ce.set_placeholder_text(
            "/terms   (must start with one of: / @ # $ & * !)")
        ce.set_activates_default(True)
        ce.set_text(cmd or "")
        box.pack_start(ce, False, False, 0)
        dlg.set_default_response(Gtk.ResponseType.OK)

        dlg.show_all()
        result = None
        while True:
            resp = dlg.run()
            if resp != Gtk.ResponseType.OK:
                break
            plain_txt = tv.get_buffer().get_text(
                *tv.get_buffer().get_bounds(),
                include_hidden_chars=False).strip()
            rich_plain = buf.get_text(
                *buf.get_bounds(), include_hidden_chars=False).strip()
            has_rich = bool(rich_plain)
            html_txt = serialize_rich(buf, "html") if has_rich else None
            if has_rich and not plain_txt:
                plain_txt = html.unescape(re.sub(
                    r"<[^>]+>", "",
                    html_txt.replace("<br>", "\n"))).strip()
            raw_cmd = ce.get_text().strip()
            the_cmd = normalize_cmd(raw_cmd) if raw_cmd else None
            if raw_cmd and not the_cmd:
                self._set_status("Invalid short command — must start "
                                 "with one of / @ # $ & * ! then "
                                 "letters/digits.")
                continue
            if not plain_txt and not has_rich:
                self._set_status("Text is empty.")
                continue
            result = {"plain": plain_txt, "html": html_txt,
                      "cmd": the_cmd, "attach": list(attach_files)}
            break
        dlg.destroy()
        return result

    def _add_text(self):
        res = self._text_editor_dialog("Add text")
        if res is None:
            return
        if not res["plain"] and not res["html"]:
            self._set_status("Text is empty — nothing added.")
            return
        e = {"kind": "text", "text": res["plain"], "ts": time.time(),
             "pinned": True}
        if res["html"]:
            e["html"] = res["html"]
        if res["attach"]:
            e["attach"] = res["attach"]
        if res["cmd"]:
            e["cmd"] = res["cmd"]
        key = entry_key(e)
        self.daemon.history = [h for h in self.daemon.history
                               if entry_key(h) != key]
        self.daemon.history.insert(0, e)
        self.daemon.save()
        self.refresh_all()
        self._set_status("Text added." +
                         (f" Type {res['cmd']} anywhere to expand it."
                          if res["cmd"] else ""))

    def _add_files(self):
        dlg = Gtk.FileChooserDialog(
            title="Add files / media", transient_for=self,
            action=Gtk.FileChooserAction.OPEN)
        dlg.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                        Gtk.STOCK_ADD, Gtk.ResponseType.OK)
        dlg.set_select_multiple(True)
        resp = dlg.run()
        filenames = list(dlg.get_filenames())
        dlg.destroy()
        if resp != Gtk.ResponseType.OK or not filenames:
            return
        os.makedirs(IMAGE_DIR, exist_ok=True)
        added = 0
        for path in filenames:
            uri = "file://" + quote(os.path.abspath(path))
            name = os.path.basename(path)
            ext = os.path.splitext(path)[1].lower()
            if ext in IMG_EXTS:
                try:
                    with open(path, "rb") as f:
                        digest = hashlib.sha1(f.read()).hexdigest()[:16]
                    fn = digest + ext
                    dst = os.path.join(IMAGE_DIR, fn)
                    if not os.path.exists(dst):
                        with open(path, "rb") as s, open(dst, "wb") as d:
                            d.write(s.read())
                    pb = GdkPixbuf.Pixbuf.new_from_file(dst)
                    e = {"kind": "image", "file": fn,
                         "w": pb.get_width(), "h": pb.get_height(),
                         "name": name, "ts": time.time(), "pinned": True}
                except Exception:
                    continue
            else:
                e = {"kind": "files", "uris": [uri], "name": name,
                     "ts": time.time(), "pinned": True}
            key = entry_key(e)
            self.daemon.history = [h for h in self.daemon.history
                                   if entry_key(h) != key]
            self.daemon.history.insert(0, e)
            added += 1
        self.daemon.save()
        self.refresh_all()
        self._set_status(f"Added {added} item(s).")

    def _set_cmd(self, e):
        dlg = Gtk.Dialog(title="Short command", transient_for=self,
                         modal=True)
        dlg.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                        Gtk.STOCK_OK, Gtk.ResponseType.OK)
        box = dlg.get_content_area()
        box.set_border_width(10)
        box.set_spacing(6)
        hint = Gtk.Label(
            label="Type this command anywhere and SuperV offers to expand "
                  "it into this content.\nMust start with one of "
                  "/ @ # $ & * ! followed by letters/digits.\n"
                  "Leave empty to remove the command.", xalign=0)
        hint.get_style_context().add_class("clip-time")
        hint.set_line_wrap(True)
        box.pack_start(hint, False, False, 0)
        entry = Gtk.Entry()
        entry.set_text(e.get("cmd", ""))
        entry.set_activates_default(True)
        box.pack_start(entry, False, False, 0)
        dlg.set_default_response(Gtk.ResponseType.OK)
        dlg.show_all()
        while True:
            resp = dlg.run()
            if resp != Gtk.ResponseType.OK:
                break
            raw = entry.get_text().strip()
            if not raw:
                e.pop("cmd", None)
                self._set_status("Short command removed.")
                break
            cmd = normalize_cmd(raw)
            if not cmd:
                self._set_status("Invalid short command — must start with "
                                 "one of / @ # $ & * ! then letters/digits.")
                continue
            for h in self.daemon.history:
                if h is not e and h.get("cmd") == cmd:
                    self._set_status(f"'{cmd}' is already assigned to "
                                     "another item.")
                    cmd = None
                    break
            if cmd:
                e["cmd"] = cmd
                self._set_status(f"Assigned '{cmd}'.")
                break
        dlg.destroy()
        self.daemon.save()
        self.refresh_all()

    def _delete(self, e):
        for i, h in enumerate(self.daemon.history):
            if h is e:
                del self.daemon.history[i]
                break
        self.daemon.save()
        self._set_status("Deleted.")
        self.refresh_all()

    def _pin(self, e):
        e["pinned"] = not e.get("pinned", False)
        self.daemon.save()
        self._set_status("Pinned." if e["pinned"] else "Unpinned.")
        self.refresh_all()

    def _move(self, e, group):
        e["group"] = group
        self.daemon.save()
        self._set_status(f"Moved to {group or '(unsorted)'}.")
        self.refresh_all()

    # ---------------------------------------------- folder management --
    def _prompt_folder_name(self, title, default=""):
        dlg = Gtk.Dialog(title=title, transient_for=self, modal=True)
        dlg.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                        Gtk.STOCK_OK, Gtk.ResponseType.OK)
        entry = Gtk.Entry()
        entry.set_text(default)
        entry.set_activates_default(True)
        dlg.set_default_response(Gtk.ResponseType.OK)
        dlg.get_content_area().set_border_width(10)
        dlg.get_content_area().pack_start(entry, False, False, 4)
        dlg.show_all()
        resp = dlg.run()
        name = entry.get_text().strip()
        dlg.destroy()
        return name if resp == Gtk.ResponseType.OK and name else None

    def _new_folder(self):
        name = self._prompt_folder_name("New folder")
        if not name or name in self.daemon.folders:
            return
        self.daemon.folders.append(name)
        save_folders(self.daemon.folders)
        self.refresh_all()
        self._set_status(f"Folder '{name}' created.")

    def _rename_folder(self, gid=None):
        old = gid or (self.current_group if self.current_group not in
                      ("__all__", None) else None)
        if not old:
            self._set_status("Right-click a folder to rename it.")
            return
        new = self._prompt_folder_name("Rename folder", old)
        if not new or new == old:
            return
        if new in self.daemon.folders:
            self._set_status("A folder with that name exists.")
            return
        idx = self.daemon.folders.index(old)
        self.daemon.folders[idx] = new
        save_folders(self.daemon.folders)
        for h in self.daemon.history:
            if h.get("group") == old:
                h["group"] = new
        self.daemon.save()
        if self.current_group == old:
            self.current_group = new
        self.refresh_all()
        self._set_status(f"Renamed to '{new}'.")

    def _delete_folder(self, gid=None):
        old = gid or (self.current_group if self.current_group not in
                      ("__all__", None) else None)
        if not old:
            self._set_status("Right-click a folder to delete it.")
            return
        if old in self.daemon.folders:
            self.daemon.folders.remove(old)
            save_folders(self.daemon.folders)
        for h in self.daemon.history:
            if h.get("group") == old:
                h["group"] = None
        self.daemon.save()
        self.current_group = "__all__"
        self.refresh_all()
        self._set_status(f"Folder '{old}' deleted; items kept unsorted.")

    # ------------------------------------------------ import / export --
    def _export_data(self):
        dlg = Gtk.FileChooserDialog(
            title="Export data", transient_for=self,
            action=Gtk.FileChooserAction.SAVE)
        dlg.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                        Gtk.STOCK_SAVE, Gtk.ResponseType.OK)
        dlg.set_do_overwrite_confirmation(True)
        dlg.set_current_name("superv-export.json")
        resp = dlg.run()
        path = dlg.get_filename()
        dlg.destroy()
        if resp != Gtk.ResponseType.OK or not path:
            return
        try:
            data = {"folders": self.daemon.folders, "history": []}
            for e in self.daemon.history:
                c = dict(e)
                if entry_kind(e) == "image":
                    p = os.path.join(IMAGE_DIR, e.get("file", ""))
                    try:
                        with open(p, "rb") as f:
                            c["png_b64"] = base64.b64encode(f.read()).decode()
                    except OSError:
                        pass
                data["history"].append(c)
            with open(path, "w") as f:
                json.dump(data, f)
            self._set_status(f"Exported {len(data['history'])} items.")
        except Exception as ex:
            self._set_status(f"Export failed: {ex}")

    def _import_data(self):
        dlg = Gtk.FileChooserDialog(
            title="Import data", transient_for=self,
            action=Gtk.FileChooserAction.OPEN)
        dlg.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                        Gtk.STOCK_OPEN, Gtk.ResponseType.OK)
        resp = dlg.run()
        path = dlg.get_filename()
        dlg.destroy()
        if resp != Gtk.ResponseType.OK or not path:
            return
        try:
            with open(path) as f:
                data = json.load(f)
            hist = data.get("history", [])
            folders = data.get("folders", [])
            if not isinstance(hist, list):
                raise ValueError("bad file format")
            added = 0
            for e in hist:
                if not isinstance(e, dict):
                    continue
                b64 = e.pop("png_b64", None)
                if e.get("kind") == "image" and b64 and e.get("file"):
                    os.makedirs(IMAGE_DIR, exist_ok=True)
                    with open(os.path.join(IMAGE_DIR, e["file"]), "wb") as f:
                        f.write(base64.b64decode(b64))
                e.setdefault("pinned", False)
                e.setdefault("kind", "text")
                e.setdefault("ts", time.time())
                key = entry_key(e)
                self.daemon.history = [h for h in self.daemon.history
                                       if entry_key(h) != key]
                self.daemon.history.insert(0, e)
                added += 1
            for g in folders:
                if isinstance(g, str) and g not in self.daemon.folders:
                    self.daemon.folders.append(g)
            if MAX_ITEMS > 0:
                self.daemon.history[:] = _trim_to_max(self.daemon.history)
            self.daemon.save()
            save_folders(self.daemon.folders)
            self.refresh_all()
            self._set_status(f"Imported {added} items.")
        except Exception as ex:
            self._set_status(f"Import failed: {ex}")

    def refresh_all(self):
        self.refresh_folders()
        self.refresh_items()


# --------------------------------------------------------- text expander --
class SuggestPopup(Gtk.Window):
    """Small non-focusable bubble listing matching short commands.
    Clicks work on it, but it never steals the keyboard focus."""

    MAX_ROWS = 6

    def __init__(self, on_pick):
        super().__init__()
        self.set_decorated(False)
        self.set_resizable(False)
        self.set_keep_above(True)
        self.set_accept_focus(False)   # never steal the typing focus
        self.set_focus_on_map(False)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        try:
            self.set_type_hint(Gdk.WindowTypeHint.NOTIFICATION)
        except Exception:
            pass
        self.get_style_context().add_class("sug-window")
        self.on_pick = on_pick
        self.vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(self.vbox)

    def set_items(self, items, sel):
        for c in self.vbox.get_children():
            self.vbox.remove(c)
        for idx, (cmd, entry) in enumerate(items[:self.MAX_ROWS]):
            row = Gtk.EventBox()
            row.get_style_context().add_class("sug-row")
            if idx == sel:
                row.get_style_context().add_class("sug-sel")
            hb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                         spacing=8)
            hb.pack_start(make_icon("zap", 13, (1.0, 0.78, 0.33)),
                          False, False, 0)
            cl = Gtk.Label(label=cmd, xalign=0)
            cl.get_style_context().add_class("sug-cmd")
            hb.pack_start(cl, False, False, 0)
            pl = Gtk.Label(label=entry_preview(entry, 32), xalign=0)
            pl.set_ellipsize(Pango.EllipsizeMode.END)
            pl.set_max_width_chars(28)
            pl.get_style_context().add_class("clip-time")
            hb.pack_start(pl, True, True, 0)
            row.add(hb)
            row.connect("button-press-event",
                        lambda _eb, _ev, i=idx: self.on_pick(i))
            self.vbox.pack_start(row, False, False, 0)
        hint = Gtk.Label(
            label="↑↓ select · ⏎ expand · Esc ignore · click a row",
            xalign=0.5)
        hint.get_style_context().add_class("sug-hint")
        self.vbox.pack_start(hint, False, False, 0)
        self.show_all()

    def set_selected(self, sel):
        rows = [c for c in self.vbox.get_children()
                if isinstance(c, Gtk.EventBox)]
        for i, r in enumerate(rows):
            sc = r.get_style_context()
            sc.remove_class("sug-sel")
            if i == sel:
                sc.add_class("sug-sel")

    def present_at_pointer(self):
        self.show_all()
        x = y = None
        disp = Gdk.Display.get_default()
        seat = disp.get_default_seat() if disp else None
        if seat is not None:
            dev = seat.get_pointer()
            if dev is not None:
                _screen, mx, my = dev.get_position()
                # stay tight to the cursor / text caret
                x, y = mx + 10, my + 16
        if x is None:
            scr = Gdk.Screen.get_default()
            x, y = scr.get_width() // 2 - 170, scr.get_height() // 2 - 60
        self.move(x, y)


class KeyWatcher(threading.Thread):
    """Global keystroke monitor built on the X11 RECORD extension.
    Runs as a daemon thread; reports every printable key through cb().
    cb receives '\\b' (backspace), '\\n' (return), '\\x1b' (escape),
    'UP'/'DOWN' (arrows), None (ignored/cleared key), or one character."""

    KEY_RETURN = (65293, 65421)
    KEY_BACKSPACE = 65288
    KEY_ESCAPE = 65307
    KEY_UP = 65362
    KEY_DOWN = 65364

    def __init__(self, cb):
        super().__init__(daemon=True)
        self.cb = cb

    def run(self):
        try:
            from Xlib import display as xdisplay, X
            from Xlib.ext import record
            from Xlib.protocol import rq
        except ImportError as ex:
            print(f"SuperV text expander needs python3-xlib ({ex})",
                  flush=True)
            return
        local = xdisplay.Display()
        rec = xdisplay.Display()

        def handler(reply):
            if reply.category != record.FromServer or reply.client_swapped:
                return
            data = reply.data
            while data:
                event, data = rq.EventField(None).parse_binary_value(
                    data, rec.display, None, None)
                if event.type != X.KeyPress:
                    continue
                state = event.state
                if state & (X.ControlMask | X.Mod1Mask | X.Mod4Mask):
                    self.cb(None)
                    continue
                idx = 1 if state & X.ShiftMask else 0
                try:
                    ks = local.keycode_to_keysym(event.detail, idx)
                except Exception:
                    continue
                if ks in self.KEY_RETURN:
                    self.cb("\n")
                elif ks == self.KEY_BACKSPACE:
                    self.cb("\b")
                elif ks == self.KEY_ESCAPE:
                    self.cb("\x1b")
                elif ks == self.KEY_UP:
                    self.cb("UP")
                elif ks == self.KEY_DOWN:
                    self.cb("DOWN")
                elif 32 <= ks < 256:
                    self.cb(chr(ks))
                elif 0x100 <= ks <= 0x24F or ks >= 0x1000000:
                    # Latin Extended-A / Unicode keysyms (accented letters,
                    # non-latin layouts) — don't reset the buffer on them
                    cp = ks - 0x1000000 if ks >= 0x1000000 else ks
                    if 0 < cp < 0x110000:
                        self.cb(chr(cp))
                    else:
                        self.cb(None)
                else:
                    self.cb(None)

        rng = {"core_requests": (0, 0), "core_replies": (0, 0),
               "ext_requests": (0, 0, 0, 0), "ext_replies": (0, 0, 0, 0),
               "delivered_events": (0, 0), "device_events": (2, 3),
               "errors": (0, 0), "client_started": False,
               "client_died": False}
        try:
            ctx = rec.record_create_context(
                0, [record.CurrentClients], [rng])
            rec.record_enable_context(ctx, handler)
            rec.record_free_context(ctx)
        except Exception as ex:
            print(f"SuperV text expander failed to start: {ex}", flush=True)


class Expander:
    """Watches global typing and offers to expand assigned short commands.
    Suggests prefix matches from the second typed character onwards."""

    BUF_MAX = 48

    def __init__(self, daemon):
        self.daemon = daemon
        self.map = {}            # cmd -> entry
        self.buf = ""
        self.lock = threading.Lock()
        self.win = None          # SuggestPopup, created on the GTK thread
        self.cands = []          # [(cmd, entry), ...] currently shown
        self.sel = 0
        self.visible = False
        self.kbd_grabbed = False
        self.entered = False      # accept was triggered by Enter

    # ---- wiring ----
    def start(self):
        self.refresh_map()
        KeyWatcher(self.on_key).start()

    def refresh_map(self):
        m = {}
        for h in reversed(self.daemon.history):   # newest assignment wins
            c = h.get("cmd")
            if c:
                m[c] = h
        self.map = m

    # ---- keystroke side (record thread) ----
    def on_key(self, ch):
        if ch == "\n":
            # Enter also reaches the target app (inserting a newline
            # there) — remember it so _choose deletes that newline too
            with self.lock:
                self.entered = True
            GLib.idle_add(self._choose); return
        if ch == "\x1b":
            GLib.idle_add(self._cancel); return
        if ch in ("UP", "DOWN"):
            GLib.idle_add(self._move_sel, 1 if ch == "DOWN" else -1)
            return
        with self.lock:
            if ch == "\b":
                self.buf = self.buf[:-1]
            elif ch is None:
                self.buf = ""
            else:
                if self.kbd_grabbed:
                    GLib.idle_add(self._leave_kbd)   # user kept typing
                self.buf = (self.buf + ch)[-self.BUF_MAX:]
            cands = self._candidates()
        GLib.idle_add(self._update, cands)

    def _token(self):
        b = self.buf
        i = len(b)
        while i > 0 and (b[i - 1].isalnum() or b[i - 1] == "_"
                         or b[i - 1] in CMD_PREFIXES):
            i -= 1
        tok = b[i:].lower()
        if not tok or tok[0] not in CMD_PREFIXES:
            return None
        # suggest after prefix + two characters — or an exact full command
        if len(tok) >= 3 or tok in self.map:
            return tok
        return None

    def _candidates(self):
        tok = self._token()
        if tok is None:
            return []
        out = sorted(((c, e) for c, e in self.map.items()
                      if c.lower().startswith(tok)),
                     key=lambda ce: (len(ce[0]), ce[0]))
        return out[:SuggestPopup.MAX_ROWS]

    # ---- UI / action side (GTK thread) ----
    def _update(self, cands):
        if not cands:
            if self.visible:
                self._hide()
            return False
        sel = next((i for i, (c, _e) in enumerate(cands)
                    if c.lower() == self._last_token()), 0)
        if self.visible and len(cands) == len(self.cands) \
                and all(a[0] == b[0] for a, b in zip(cands, self.cands)):
            if sel != self.sel:
                self.sel = sel
                self.win.set_selected(sel)
            return False
        self.cands = cands
        self.sel = sel
        if self.win is None:
            self.win = SuggestPopup(
                lambda i: GLib.idle_add(self._pick_index, i))
        if not self._own_window_focused():
            self.win.set_items(cands, sel)
            self.win.present_at_pointer()
            self.visible = True
        return False

    def _last_token(self):
        with self.lock:
            return self._token() or ""

    def _move_sel(self, d):
        if not self.visible or not self.cands:
            return False
        self.sel = (self.sel + d) % len(self.cands)
        self.win.set_selected(self.sel)
        if not self.kbd_grabbed:
            self._grab_kbd()
        return False

    # clicking a row expands it directly (no focus stolen, so this works)
    def _pick_index(self, i):
        if 0 <= i < len(self.cands):
            self.sel = i
            self.entered = False
            self._choose()
        return False

    def _choose(self):
        if not self.visible or not self.cands:
            self.entered = False
            return False
        cmd, entry = self.cands[self.sel]
        self._hide()
        with self.lock:
            self.buf = ""
        n_back = len(cmd) + (1 if self.entered else 0)
        had_enter = self.entered
        self.entered = False

        def erase():
            try:
                subprocess.Popen(
                    ["xdotool", "key", "--clearmodifiers"] +
                    ["BackSpace"] * n_back,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except FileNotFoundError:
                return
            # let the target app process the erasure before pasting
            GLib.timeout_add(200,
                             lambda: self.daemon.commit_and_paste(entry))
        if had_enter:
            # the Enter keypress is still in flight to the target app —
            # give it time to insert the newline before we erase
            GLib.timeout_add(140, erase)
        else:
            erase()
        return False

    def _cancel(self):
        if self.visible:
            self._hide()
        self.entered = False
        with self.lock:
            self.buf = ""
        return False

    def _hide(self):
        self._leave_kbd()
        if self.win is not None:
            self.win.hide()
        self.cands = []
        self.visible = False

    # keyboard grab so ↑/↓/⏎ don't move the caret in the target app
    def _grab_kbd(self):
        if self.kbd_grabbed or self.win is None:
            return
        win = self.win.get_window()
        if win is None:
            return
        disp = Gdk.Display.get_default()
        seat = disp.get_default_seat()
        cursor = Gdk.Cursor.new_from_name(disp, "default")
        status = seat.grab(win, Gdk.SeatCapabilities.KEYBOARD,
                           True, cursor, None, None)
        self.kbd_grabbed = status == Gdk.GrabStatus.SUCCESS

    def _leave_kbd(self):
        if self.kbd_grabbed:
            try:
                Gdk.Display.get_default().get_default_seat().ungrab()
            except Exception:
                pass
            self.kbd_grabbed = False
        return False

    def _own_window_focused(self):
        try:
            out = subprocess.run(
                ["xdotool", "getactivewindow"],
                capture_output=True, text=True, timeout=0.5)
            aid = int(out.stdout.strip())
        except Exception:
            return False
        for w in (self.daemon.popup, self.daemon.manager):
            if w is not None and w.get_visible():
                win = w.get_window()
                if win is not None and win.get_xid() == aid:
                    return True
        return False


# ------------------------------------------------------------------ daemon --
class Daemon:
    def __init__(self):
        self.history = load_history()
        self.folders = load_folders()
        self.popup = None
        self.manager = None
        self.session_keys = set()  # keys captured/pasted during this run
        self.last_key = None
        self.clip = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        self.clip.connect("owner-change", self.on_owner_change)
        self.expander = None
        Gdk.event_handler_set(self._event_filter)

    def _event_filter(self, event):
        """Close the popup when a click lands outside it.
        Pinned popups ignore outside-clicks (that's the whole point)."""
        popup = self.popup
        if (popup is not None and popup.get_visible()
                and popup.edit_dialog is None
                and not popup._menu_open
                and not popup._pinned_on_top
                and event.type == Gdk.EventType.BUTTON_PRESS):
            win = popup.get_window()
            if win is not None:
                _, ox, oy = win.get_origin()
                alloc = popup.get_allocation()
                inside = (ox <= event.x_root < ox + alloc.width
                          and oy <= event.y_root < oy + alloc.height)
                if not inside:
                    popup.dismiss("outside-click")
        Gtk.main_do_event(event)

    def save(self):
        save_history(self.history)
        if self.expander is not None:
            self.expander.refresh_map()

    def log(self, msg):
        print(time.strftime("[%H:%M:%S] ") + msg, flush=True)

    def on_owner_change(self, _clip, event):
        """Capture text, rich text (HTML), images and file lists."""
        reason = getattr(event, "reason", "?")
        self.log(f"owner-change reason={reason}")
        cap = {"text": None, "html": None, "img": None, "uris": None,
               "gnome": None, "left": 5, "done": False}

        def tick():
            cap["left"] -= 1
            if cap["left"] <= 0:
                finish()

        def finish():
            if cap["done"]:
                return
            cap["done"] = True
            self._store_captured(cap)

        def got_text(_cb, text):
            cap["text"] = text if text else None
            tick()

        def got_html(_cb, sd):
            try:
                data = sd.get_data()
                cap["html"] = bytes(data).decode("utf-8",
                                                 errors="replace") if data else None
            except Exception:
                cap["html"] = None
            tick()

        def got_uris(_cb, uris):
            cap["uris"] = [u for u in (uris or []) if u]
            tick()

        def got_gnome_files(_cb, sd):
            try:
                data = sd.get_data()
                if data:
                    lines = bytes(data).decode(
                        "utf-8", errors="replace").splitlines()
                    # format: "copy\n<uri>\n<uri>…" (or "cut\n…")
                    cap["gnome"] = [l for l in lines[1:] if l.strip()]
                else:
                    cap["gnome"] = []
            except Exception:
                cap["gnome"] = []
            tick()

        def got_image(_cb, pixbuf):
            cap["img"] = pixbuf
            tick()

        # Safety net in case a target never answers
        GLib.timeout_add(700, lambda: (finish(), False)[1])

        def safe(call):
            try:
                call()
            except Exception:
                tick()  # don't hang waiting for a failed request

        safe(lambda: self.clip.request_text(got_text))
        safe(lambda: self.clip.request_contents(
            Gdk.Atom.intern("text/html", False), got_html))
        safe(lambda: self.clip.request_uris(got_uris))
        safe(lambda: self.clip.request_contents(
            Gdk.Atom.intern("x-special/gnome-copied-files", False),
            got_gnome_files))
        safe(lambda: self.clip.request_image(got_image))

    def _store_captured(self, cap):
        entry = None
        # a two-phase attach paste suppresses capture of its own image
        if time.time() < getattr(self, "capture_hold_until", 0):
            return

        uris = _valid_uris(cap.get("uris"))
        if not uris:
            uris = _valid_uris(cap.get("gnome"))
        text = cap["text"].strip() if cap["text"] else ""
        html = cap["html"].strip() if cap["html"] else ""
        if html and "<" not in html[:200]:
            html = ""  # not real markup — ignore
        self.log(f"capture: text={bool(text)} html={bool(html)} "
                 f"img={cap['img'] is not None} uris={len(uris)}")

        if uris:
            entry = {"kind": "files", "uris": uris}
        elif cap["img"] is not None and (not text or
                                         _looks_like_image_ref(text, html)):
            try:
                name, w, h = save_image_pixbuf(cap["img"])
            except Exception:
                return
            entry = {"kind": "image", "file": name, "w": w, "h": h}
        elif text:
            entry = {"kind": "text", "text": cap["text"]}
            if html and html != text:
                entry["html"] = html
        else:
            return

        key = entry_key(entry)
        if key == self.last_key:
            return  # we put it there ourselves / duplicate of last copy

        now = time.time()
        # keep user metadata when the same content is captured again —
        # otherwise re-copying it would strip its short command,
        # attachments, pin, name and folder
        old = next((h for h in self.history if entry_key(h) == key), None)
        self.history = [h for h in self.history if entry_key(h) != key]
        entry.update({"ts": now, "pinned": False})
        if old is not None:
            for f in ("cmd", "attach", "name", "group", "pinned"):
                if f in old:
                    entry[f] = old[f]
        self.history.insert(0, entry)
        if MAX_ITEMS > 0:
            self.history[:] = _trim_to_max(self.history)
        self.last_key = key
        self.session_keys.add(key)
        self.save()
        self.log(f"stored kind={entry_kind(entry)} history={len(self.history)}")
        if self.popup is not None:
            self.popup.rebuild()
        if self.manager is not None:
            self.manager.refresh_all()

    def commit_multi(self, entries):
        """Paste several entries at once: files+images merge into one
        multi-file clipboard payload; texts are joined with newlines."""
        entries = [e for e in entries if e is not None]
        if not entries:
            return
        if len(entries) == 1:
            self.commit_and_paste(entries[0])
            return

        uris = []
        texts = []
        for e in entries:
            k = entry_kind(e)
            if k == "image":
                path = os.path.join(IMAGE_DIR, e.get("file", ""))
                if os.path.isfile(path):
                    uris.append("file://" + quote(path))
            elif k == "files":
                uris.extend(e.get("uris", []))
            else:
                texts.append(e.get("text", ""))

        payload = []
        if uris:
            payload.append(("text/uri-list", "\n".join(uris)))
            payload.append(("x-special/gnome-copied-files",
                            "copy\n" + "\n".join(uris)))
        if texts:
            joined = "\n".join(texts)
            payload.append(("UTF8_STRING", joined))
            payload.append(("text/plain;charset=utf-8", joined))
        if not payload:
            return

        if not clipboard_set_payload(self.clip, payload):
            return
        self.clip.store()
        # Prevent re-capturing our own paste
        self.last_key = ("files:" + "\n".join(uris)) if uris \
            else ("text:" + "\n".join(texts))
        if uris:
            self.session_keys.add(self.last_key)
        GLib.timeout_add(150, self._send_paste)

    def copy_entry(self, entry):
        """Put the entry on the clipboard (no auto-paste). True on success."""
        kind = entry_kind(entry)
        if kind == "image":
            try:
                pb = GdkPixbuf.Pixbuf.new_from_file(
                    os.path.join(IMAGE_DIR, entry["file"]))
                self.clip.set_image(pb)
            except Exception:
                return False
            self.clip.store()
            self.last_key = entry_key(entry)
            self.session_keys.add(self.last_key)
            return True
        if kind == "files":
            uris = entry["uris"]
            ok = clipboard_set_payload(self.clip, [
                ("text/uri-list", "\n".join(uris)),
                ("x-special/gnome-copied-files", "copy\n" + "\n".join(uris)),
            ])
            if not ok:
                return False
        elif entry.get("html"):
            text = entry.get("text", "")
            ok = clipboard_set_payload(self.clip, [
                ("UTF8_STRING", text),
                ("text/plain;charset=utf-8", text),
                ("text/html", entry["html"]),
            ])
            if not ok:
                return False
        else:
            self.clip.set_text(entry.get("text", ""), -1)
        self.clip.store()
        self.last_key = entry_key(entry)
        return True

    def commit_and_paste(self, entry):
        """Put the entry back on the clipboard and send Ctrl+V."""
        kind = entry_kind(entry)
        attach = entry.get("attach") or []
        if attach:
            text = entry.get("text", "")

            def set_clip(payload):
                if not clipboard_set_payload(self.clip, payload):
                    self.log("attach paste: clipboard_set_payload failed")
                    return False
                self.clip.store()
                self.last_key = entry_key(entry)
                return True

            if text:
                # Browsers (ChatGPT, etc.) prefer an image target over
                # text and would drop the text — paste in two phases:
                # text first, then the image(s).
                if not set_clip([("UTF8_STRING", text),
                                 ("text/plain;charset=utf-8", text)]):
                    return
                GLib.timeout_add(150, self._send_paste)

                def phase2():
                    try:
                        if len(attach) == 1:
                            with open(os.path.join(IMAGE_DIR, attach[0]),
                                      "rb") as f:
                                png = f.read()
                            payload = [("image/png", png)]
                        else:
                            uris = ["file://" + quote(
                                os.path.join(IMAGE_DIR, fn))
                                for fn in attach]
                            payload = [
                                ("text/uri-list", "\n".join(uris)),
                                ("x-special/gnome-copied-files",
                                 "copy\n" + "\n".join(uris)),
                            ]
                        # don't capture our own image back into history
                        self.capture_hold_until = time.time() + 2.5
                        if set_clip(payload):
                            GLib.timeout_add(150, self._send_paste)
                    except Exception as ex:
                        self.log(f"attach paste phase2 failed: {ex}")
                GLib.timeout_add(700, phase2)
                return
            try:
                if len(attach) == 1:
                    with open(os.path.join(IMAGE_DIR, attach[0]),
                              "rb") as f:
                        png = f.read()
                    payload = [("image/png", png)]
                else:
                    uris = ["file://" + quote(
                        os.path.join(IMAGE_DIR, fn)) for fn in attach]
                    payload = [
                        ("text/uri-list", "\n".join(uris)),
                        ("x-special/gnome-copied-files",
                         "copy\n" + "\n".join(uris)),
                    ]
                if not set_clip(payload):
                    return
            except Exception as ex:
                self.log(f"attach paste failed: {ex}")
                return
            GLib.timeout_add(150, self._send_paste)
            return
        if kind == "image":
            try:
                pb = GdkPixbuf.Pixbuf.new_from_file(
                    os.path.join(IMAGE_DIR, entry["file"]))
                self.clip.set_image(pb)
            except Exception:
                return
            self.clip.store()
            self.last_key = entry_key(entry)
        elif kind == "files":
            uris = entry["uris"]
            ok = clipboard_set_payload(self.clip, [
                ("text/uri-list", "\n".join(uris)),
                ("x-special/gnome-copied-files", "copy\n" + "\n".join(uris)),
            ])
            if not ok:
                return
            self.clip.store()
            self.last_key = entry_key(entry)
        elif entry.get("html"):
            text = entry.get("text", "")
            ok = clipboard_set_payload(self.clip, [
                ("UTF8_STRING", text),
                ("text/plain;charset=utf-8", text),
                ("text/html", entry["html"]),
            ])
            if not ok:
                return
            self.clip.store()
            self.last_key = entry_key(entry)
        else:
            self.clip.set_text(entry.get("text", ""), -1)
            self.clip.store()
            self.last_key = entry_key(entry)
        GLib.timeout_add(150, self._send_paste)

    def open_manager(self):
        self.log("open_manager called")
        try:
            if self.manager is None:
                self.manager = ManagerWindow(self)
                self.manager.connect(
                    "destroy", lambda _w: setattr(self, "manager", None))
            self.manager.refresh_all()
            self.manager.show_all()
            # show_all() reveals everything — restore intended view state
            self.manager._apply_view()
            self.manager.search.set_visible(False)
            self.manager.present()
            self.log("manager shown")
        except Exception:
            import traceback
            self.log("manager error:\n" + traceback.format_exc())
        return False

    @staticmethod
    def _send_paste():
        try:
            subprocess.Popen(["xdotool", "key", "--clearmodifiers", "ctrl+v"],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            pass
        return False

    def toggle(self):
        if self.popup is None:
            self.popup = ClipPopup(self)
        if self.popup.get_visible():
            self.popup.dismiss()
        else:
            try:
                self.popup.show_popup()
            except Exception as ex:
                # Never let a positioning error kill the daemon - log
                # and try a plain show as a last resort.
                import traceback
                self.log("show_popup failed:\n" + traceback.format_exc())
                self.popup.show_all()
                self.popup.present()

    def run(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        icon_pb = app_icon_pixbuf()
        if icon_pb is not None:
            Gtk.Window.set_default_icon(icon_pb)
        # Single-instance guard: two daemons would overwrite each other's
        # history.json and items would appear to "clean themselves".
        self._lock_file = open(os.path.join(DATA_DIR, "daemon.lock"), "w")
        try:
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("SuperV daemon already running — exiting", flush=True)
            sys.exit(0)

        # IPC: a regular file the toggle script appends to. The daemon
        # polls mtime/size to detect new commands. This is race-free
        # unlike a FIFO, which loses data when the writer closes
        # before the reader opens it (O_NONBLOCK returns EAGAIN).
        self._cmd_path = os.path.join(DATA_DIR, "command.log")
        try:
            # Truncate on start so stale commands from a previous
            # session don't fire after a crash.
            with open(self._cmd_path, "w") as f:
                pass
        except OSError:
            pass
        self._last_cmd_offset = 0

        print("SuperV daemon running "
              f"({sum(1 for h in self.history if h['pinned'])} pins loaded)",
              flush=True)

        try:
            self.expander = Expander(self)
            self.expander.start()
        except Exception as ex:
            self.log(f"text expander unavailable: {ex}")

        def check_command():
            try:
                try:
                    size = os.path.getsize(self._cmd_path)
                except OSError:
                    return True
                if size < self._last_cmd_offset:
                    # File was truncated/recreated — restart offset.
                    self._last_cmd_offset = 0
                if size == self._last_cmd_offset:
                    return True
                with open(self._cmd_path, "r") as f:
                    f.seek(self._last_cmd_offset)
                    data = f.read(size - self._last_cmd_offset)
                self._last_cmd_offset = size
                if "manager" in data:
                    GLib.idle_add(self.open_manager)
                if "toggle" in data:
                    self.log(f"command: toggle (data={data!r})")
                    try:
                        self.toggle()
                    except Exception:
                        import traceback
                        self.log("toggle failed:\n"
                                 + traceback.format_exc())
            except Exception as ex:
                self.log(f"command poll error: {ex}")
            return True
        GLib.timeout_add(100, check_command)
        Gtk.main()


if __name__ == "__main__":
    if "--version" in sys.argv or "-v" in sys.argv:
        print(f"SuperV {__version__}")
        sys.exit(0)
    Daemon().run()
