#!/usr/bin/env python3
"""Build a properly styled Word document (.docx) from a Markdown design doc.

Standard library only (no pandoc / python-docx needed).

Supports: title (first H1), H2/H3 headings, auto Table of Contents, pipe tables
(header row shading, repeating headers, key/value tables), bullet and numbered
lists (with nesting), fenced code blocks, block quotes, images with captions,
inline **bold**, *italic* and `code`, header/footer with page numbers.

Usage:
    python3 build_docx.py Maxis_Voice_Agent_Design.md [output.docx]
"""
import datetime
import re
import struct
import sys
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

# ---------------------------------------------------------------- layout ---
PAGE_W, PAGE_H = 11906, 16838  # A4 in twips
MARGIN = 1440
TEXT_W = PAGE_W - 2 * MARGIN
EMU_PER_TWIP = 635
MAX_IMG_H = 11000  # twips

FONT = "Calibri"
MONO = "Consolas"
ACCENT = "1F3864"
ACCENT2 = "2E74B5"
ACCENT_LIGHT = "DCE6F2"
ZEBRA = "F5F7FA"
BORDER = "BFBFBF"

NS = (
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
    'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"'
)
XML_HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'


# ------------------------------------------------------------ primitives ---
def run(text, bold=False, ital=False, code=False, color=None, size=None):
    rpr = ""
    if code:
        rpr += '<w:rStyle w:val="InlineCode"/>'
    if bold:
        rpr += "<w:b/><w:bCs/>"
    if ital:
        rpr += "<w:i/><w:iCs/>"
    if color:
        rpr += f'<w:color w:val="{color}"/>'
    if size:
        rpr += f'<w:sz w:val="{size}"/><w:szCs w:val="{size}"/>'
    rpr = f"<w:rPr>{rpr}</w:rPr>" if rpr else ""
    return f'<w:r>{rpr}<w:t xml:space="preserve">{escape(text)}</w:t></w:r>'


def runs(text, color=None, size=None, force_bold=False):
    """Convert inline markdown (**bold**, *italic*, `code`) into runs."""
    out, buf = [], []
    bold = ital = False
    i = 0

    def flush():
        if buf:
            out.append(run("".join(buf), bold or force_bold, ital, False, color, size))
            buf.clear()

    while i < len(text):
        ch = text[i]
        if ch == "`":
            j = text.find("`", i + 1)
            if j != -1:
                flush()
                out.append(run(text[i + 1:j], bold or force_bold, ital, True, color, size))
                i = j + 1
                continue
        if text.startswith("**", i):
            flush()
            bold = not bold
            i += 2
            continue
        if ch == "*":
            flush()
            ital = not ital
            i += 1
            continue
        buf.append(ch)
        i += 1
    flush()
    return "".join(out)


def plain(text):
    return re.sub(r"[*`]", "", text)


def para(inner="", style=None, keep_next=False, num=None, spacing=None,
         ind=None, jc=None):
    ppr = ""
    if style:
        ppr += f'<w:pStyle w:val="{style}"/>'
    if keep_next:
        ppr += "<w:keepNext/>"
    if num:
        ppr += f'<w:numPr><w:ilvl w:val="{num[1]}"/><w:numId w:val="{num[0]}"/></w:numPr>'
    if spacing:
        ppr += f"<w:spacing {spacing}/>"
    if ind:
        ppr += f"<w:ind {ind}/>"
    if jc:
        ppr += f'<w:jc w:val="{jc}"/>'
    ppr = f"<w:pPr>{ppr}</w:pPr>" if ppr else ""
    return f"<w:p>{ppr}{inner}</w:p>"


PAGE_BREAK = '<w:p><w:r><w:br w:type="page"/></w:r></w:p>'
SPACER = para(spacing='w:before="0" w:after="60"')


# ---------------------------------------------------------------- images ---
def image_size(data):
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return struct.unpack(">II", data[16:24])
    if data[:2] == b"\xff\xd8":
        i = 2
        while i < len(data) - 9:
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xD8, 0x01, 0xFF) or 0xD0 <= marker <= 0xD7:
                i += 2 if marker != 0xFF else 1
                continue
            seg_len = struct.unpack(">H", data[i + 2:i + 4])[0]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                          0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return w, h
            i += 2 + seg_len
    raise ValueError("Unsupported image format (use PNG or JPEG)")


def drawing(rid, pid, name, descr, w_px, h_px):
    w_tw = TEXT_W
    h_tw = int(w_tw * h_px / w_px)
    if h_tw > MAX_IMG_H:
        w_tw = int(w_tw * MAX_IMG_H / h_tw)
        h_tw = MAX_IMG_H
    cx, cy = w_tw * EMU_PER_TWIP, h_tw * EMU_PER_TWIP
    d = escape(descr, {'"': "&quot;"})
    return (
        f'<w:r><w:drawing><wp:inline distT="0" distB="0" distL="0" distR="0">'
        f'<wp:extent cx="{cx}" cy="{cy}"/><wp:effectExtent l="0" t="0" r="0" b="0"/>'
        f'<wp:docPr id="{pid}" name="Picture {pid}" descr="{d}"/>'
        f'<wp:cNvGraphicFramePr><a:graphicFrameLocks noChangeAspect="1"/></wp:cNvGraphicFramePr>'
        f'<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
        f'<pic:pic><pic:nvPicPr><pic:cNvPr id="{pid}" name="{escape(name)}"/><pic:cNvPicPr/></pic:nvPicPr>'
        f'<pic:blipFill><a:blip r:embed="{rid}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>'
        f'<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
        f'<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr></pic:pic>'
        f'</a:graphicData></a:graphic></wp:inline></w:drawing></w:r>'
    )


# ---------------------------------------------------------------- tables ---
def split_row(row):
    row = row.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|"):
        row = row[:-1]
    return [c.strip() for c in row.split("|")]


def column_widths(rows, ncols):
    weights, mins = [], []
    for c in range(ncols):
        cells = [plain(r[c]) for r in rows if c < len(r) and r[c]]
        lens = [len(x) for x in cells] or [4]
        avg, mx = sum(lens) / len(lens), max(lens)
        weights.append(min(mx, 14) + avg * 0.8)
        longest_word = max((len(w) for x in cells for w in x.split()), default=4)
        mins.append(min(longest_word * 115 + 260, TEXT_W // ncols))
    total = sum(weights)
    widths = [TEXT_W * w / total for w in weights]
    for _ in range(ncols):  # enforce minimums, rescale the rest
        fixed = [i for i in range(ncols) if widths[i] < mins[i]]
        if not fixed:
            break
        for i in fixed:
            widths[i] = mins[i]
        rest = [i for i in range(ncols) if i not in fixed]
        remaining = TEXT_W - sum(widths[i] for i in fixed)
        rest_total = sum(weights[i] for i in rest) or 1
        for i in rest:
            widths[i] = remaining * weights[i] / rest_total
    widths = [int(w) for w in widths]
    widths[-1] += TEXT_W - sum(widths)
    return widths


def cell(content, width, fill=None, valign="top"):
    shd = f'<w:shd w:val="clear" w:color="auto" w:fill="{fill}"/>' if fill else ""
    return (f'<w:tc><w:tcPr><w:tcW w:w="{width}" w:type="dxa"/>{shd}'
            f'<w:vAlign w:val="{valign}"/></w:tcPr>'
            f'{para(content, style="TableText")}</w:tc>')


def table(lines):
    rows = [split_row(l) for l in lines]
    rows = [r for r in rows if not all(re.fullmatch(r":?-+:?", c) for c in r)]
    header, body = rows[0], rows[1:]
    key_value = all(not c for c in header)
    ncols = max(len(r) for r in rows)
    widths = column_widths(body if key_value else rows, ncols)
    if key_value:
        widths = [int(TEXT_W * 0.25), TEXT_W - int(TEXT_W * 0.25)] if ncols == 2 else widths

    b = f'w:val="single" w:sz="4" w:space="0" w:color="{BORDER}"'
    xml = ['<w:tbl><w:tblPr>'
           f'<w:tblW w:w="{TEXT_W}" w:type="dxa"/>'
           f'<w:tblBorders><w:top {b}/><w:left {b}/><w:bottom {b}/><w:right {b}/>'
           f'<w:insideH {b}/><w:insideV {b}/></w:tblBorders>'
           '<w:tblLayout w:type="fixed"/>'
           '<w:tblCellMar><w:top w:w="70" w:type="dxa"/><w:left w:w="110" w:type="dxa"/>'
           '<w:bottom w:w="70" w:type="dxa"/><w:right w:w="110" w:type="dxa"/></w:tblCellMar>'
           '<w:tblLook w:val="04A0" w:firstRow="1" w:lastRow="0" w:firstColumn="0" '
           'w:lastColumn="0" w:noHBand="0" w:noVBand="1"/>'
           '</w:tblPr><w:tblGrid>']
    xml += [f'<w:gridCol w:w="{w}"/>' for w in widths]
    xml.append("</w:tblGrid>")

    if not key_value:
        xml.append('<w:tr><w:trPr><w:cantSplit/><w:tblHeader/></w:trPr>')
        for c in range(ncols):
            txt = header[c] if c < len(header) else ""
            xml.append(cell(runs(txt, color="FFFFFF", force_bold=True), widths[c],
                            fill=ACCENT, valign="center"))
        xml.append("</w:tr>")

    for n, r in enumerate(body):
        xml.append('<w:tr><w:trPr><w:cantSplit/></w:trPr>')
        for c in range(ncols):
            txt = r[c] if c < len(r) else ""
            if key_value and c == 0:
                xml.append(cell(runs(txt, color=ACCENT, force_bold=True), widths[c],
                                fill=ACCENT_LIGHT))
            else:
                xml.append(cell(runs(txt), widths[c], fill=ZEBRA if n % 2 else None))
        xml.append("</w:tr>")
    xml.append("</w:tbl>")
    return "".join(xml)


# ------------------------------------------------------------ converter ---
BLOCK_START = re.compile(r"^\s*(#{1,6}\s|```|\||>|!\[|---\s*$|([-*+]|\d+\.)\s)")


class Converter:
    def __init__(self, md_path):
        self.md_path = Path(md_path)
        self.lines = self.md_path.read_text(encoding="utf-8").splitlines()
        self.body = []
        self.media = []        # (rid, zip_name, bytes)
        self.ordered_nums = []  # numIds for numbered lists
        self.next_num = 2
        self.title = self.md_path.stem.replace("_", " ")
        self.version = ""
        self.figure = 0

    # -- helpers
    def toc_entries(self):
        entries, in_code = [], False
        for l in self.lines:
            if l.strip().startswith("```"):
                in_code = not in_code
            elif not in_code and l.startswith("## "):
                entries.append(plain(l[3:].strip()))
        return entries

    def toc(self):
        out = [para(run("Contents"), style="TOCHeading")]
        out.append(
            '<w:p><w:pPr><w:pStyle w:val="TOC1"/></w:pPr>'
            '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
            '<w:r><w:instrText xml:space="preserve"> TOC \\o "1-2" \\h \\z \\u </w:instrText></w:r>'
            '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
            f'{run(self.toc_entries()[0] if self.toc_entries() else "")}</w:p>')
        for e in self.toc_entries()[1:]:
            out.append(para(run(e), style="TOC1"))
        out.append('<w:p><w:pPr><w:pStyle w:val="TOC1"/></w:pPr>'
                   '<w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>')
        return out

    def code_block(self, code_lines):
        if not code_lines:
            return
        last = len(code_lines) - 1
        for n, l in enumerate(code_lines):
            r = f'<w:r><w:t xml:space="preserve">{escape(l)}</w:t></w:r>' if l else ""
            self.body.append(para(r, style="Code",
                                  spacing='w:before="0" w:after="200"' if n == last else None))

    def image(self, alt, src):
        path = (self.md_path.parent / src).resolve()
        data = path.read_bytes()
        w, h = image_size(data)
        idx = len(self.media) + 1
        rid = f"rIdImg{idx}"
        ext = path.suffix.lower().lstrip(".").replace("jpg", "jpeg")
        zip_name = f"media/image{idx}.{ 'jpg' if ext == 'jpeg' else ext}"
        self.media.append((rid, zip_name, data))
        self.body.append(para(drawing(rid, idx, path.name, alt, w, h), jc="center",
                              keep_next=True, spacing='w:before="120" w:after="60"'))
        self.figure += 1
        if alt:
            self.body.append(para(run(f"Figure {self.figure}: {alt}"), style="Caption"))

    # -- main loop
    def convert(self):
        L = self.lines
        i, title_done, hr_seen, list_ctx = 0, False, 0, None
        while i < len(L):
            line, s = L[i], L[i].strip()

            if s.startswith("```"):
                code, i = [], i + 1
                while i < len(L) and not L[i].strip().startswith("```"):
                    code.append(L[i].rstrip())
                    i += 1
                i += 1
                self.code_block(code)
                list_ctx = None
                continue

            if not s:
                i += 1
                continue

            if re.fullmatch(r"-{3,}|\*{3,}", s):
                hr_seen += 1
                if hr_seen == 1:  # end of front matter → TOC + new page
                    self.body.extend(self.toc())
                    self.body.append(PAGE_BREAK)
                list_ctx, i = None, i + 1
                continue

            m = re.match(r"^(#{1,6})\s+(.*)$", s)
            if m:
                level, text = len(m.group(1)), m.group(2).strip()
                if level == 1 and not title_done:
                    self.title = plain(text)
                    self.body.append(para(runs(text), style="Title"))
                    title_done = True
                else:
                    style = f"Heading{max(1, min(level - 1, 3))}"
                    self.body.append(para(runs(text), style=style))
                list_ctx, i = None, i + 1
                continue

            if s.startswith("|"):
                rows = []
                while i < len(L) and L[i].strip().startswith("|"):
                    rows.append(L[i])
                    i += 1
                for r in rows:
                    cells = split_row(r)
                    if len(cells) == 2 and plain(cells[0]).lower() == "version":
                        self.version = plain(cells[1])
                self.body.append(table(rows))
                self.body.append(SPACER)
                list_ctx = None
                continue

            m = re.match(r"^!\[([^\]]*)\]\(([^)]+)\)", s)
            if m:
                self.image(m.group(1), m.group(2))
                list_ctx, i = None, i + 1
                continue

            if s.startswith(">"):
                quote = []
                while i < len(L) and L[i].strip().startswith(">"):
                    quote.append(L[i].strip().lstrip(">").strip())
                    i += 1
                self.body.append(para(runs(" ".join(quote)), style="Quote"))
                list_ctx = None
                continue

            m = re.match(r"^(\s*)([-*+]|\d+\.)\s+(.*)$", line)
            if m:
                indent = len(m.group(1))
                lvl = 0 if indent < 2 else (1 if indent < 5 else 2)
                ordered = m.group(2)[0].isdigit()
                if lvl == 0:
                    kind = "ol" if ordered else "ul"
                    if not list_ctx or list_ctx["kind"] != kind:
                        if ordered:
                            list_ctx = {"kind": "ol", "num": self.next_num}
                            self.ordered_nums.append(self.next_num)
                            self.next_num += 1
                        else:
                            list_ctx = {"kind": "ul", "num": 1}
                elif list_ctx is None:
                    list_ctx = {"kind": "ul", "num": 1}
                self.body.append(para(runs(m.group(3).strip()), style="ListParagraph",
                                      num=(list_ctx["num"], lvl)))
                i += 1
                continue

            if line.startswith(" ") and list_ctx:  # continuation text inside a list
                self.body.append(para(runs(s), style="ListParagraph", ind='w:left="400"',
                                      spacing='w:before="60" w:after="60"'))
                i += 1
                continue

            # normal paragraph (join soft-wrapped lines)
            buf, i = [s], i + 1
            while i < len(L) and L[i].strip() and not BLOCK_START.match(L[i]):
                buf.append(L[i].strip())
                i += 1
            text = " ".join(buf)
            list_ctx = None
            if re.fullmatch(r"\*\*[^*]+\*\*", text):
                self.body.append(para(runs(text), style="Label"))
            else:
                self.body.append(para(runs(text)))

    # -- package parts
    def document_xml(self):
        sect = ('<w:sectPr><w:headerReference w:type="default" r:id="rIdHeader1"/>'
                '<w:footerReference w:type="default" r:id="rIdFooter1"/>'
                f'<w:pgSz w:w="{PAGE_W}" w:h="{PAGE_H}"/>'
                f'<w:pgMar w:top="{MARGIN}" w:right="{MARGIN}" w:bottom="{MARGIN}" '
                f'w:left="{MARGIN}" w:header="708" w:footer="708" w:gutter="0"/>'
                '<w:titlePg/></w:sectPr>')
        return f'{XML_HEAD}<w:document {NS}><w:body>{"".join(self.body)}{sect}</w:body></w:document>'

    def header_xml(self):
        p = para(run(self.title), style="Header", jc="right")
        return f"{XML_HEAD}<w:hdr {NS}>{p}</w:hdr>"

    def footer_xml(self):
        left = f"Version {self.version}" if self.version else self.title
        p = ('<w:p><w:pPr><w:pStyle w:val="Footer"/></w:pPr>'
             f'{run(left)}<w:r><w:tab/></w:r>{run("Page ")}'
             '<w:fldSimple w:instr=" PAGE "><w:r><w:t>1</w:t></w:r></w:fldSimple>'
             f'{run(" of ")}'
             '<w:fldSimple w:instr=" NUMPAGES "><w:r><w:t>1</w:t></w:r></w:fldSimple></w:p>')
        return f"{XML_HEAD}<w:ftr {NS}>{p}</w:ftr>"

    def numbering_xml(self):
        def lvl(i, fmt, text, left, hanging=280, rpr=""):
            return (f'<w:lvl w:ilvl="{i}"><w:start w:val="1"/><w:numFmt w:val="{fmt}"/>'
                    f'<w:lvlText w:val="{text}"/><w:lvlJc w:val="left"/>'
                    f'<w:pPr><w:ind w:left="{left}" w:hanging="{hanging}"/></w:pPr>'
                    f'<w:rPr>{rpr}</w:rPr></w:lvl>')
        font = f'<w:rFonts w:ascii="{FONT}" w:hAnsi="{FONT}"/>'
        bullets = [lvl(0, "bullet", "•", 400, rpr=font + f'<w:color w:val="{ACCENT2}"/>'),
                   lvl(1, "bullet", "–", 800, rpr=font),
                   lvl(2, "bullet", "▪", 1200, rpr=font)]
        decimal = [lvl(0, "decimal", "%1.", 400, 340,
                       rpr=f'<w:b/><w:color w:val="{ACCENT}"/>'),
                   lvl(1, "bullet", "–", 800, rpr=font),
                   lvl(2, "bullet", "▪", 1200, rpr=font)]
        xml = [f'{XML_HEAD}<w:numbering {NS}>',
               '<w:abstractNum w:abstractNumId="0"><w:multiLevelType w:val="hybridMultilevel"/>',
               *bullets, "</w:abstractNum>",
               '<w:abstractNum w:abstractNumId="1"><w:multiLevelType w:val="hybridMultilevel"/>',
               *decimal, "</w:abstractNum>",
               '<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>']
        for n in self.ordered_nums:
            xml.append(f'<w:num w:numId="{n}"><w:abstractNumId w:val="1"/>'
                       '<w:lvlOverride w:ilvl="0"><w:startOverride w:val="1"/></w:lvlOverride></w:num>')
        xml.append("</w:numbering>")
        return "".join(xml)

    @staticmethod
    def styles_xml():
        def style(sid, name, ppr="", rpr="", kind="paragraph", based="Normal", extra=""):
            b = f'<w:basedOn w:val="{based}"/>' if based else ""
            return (f'<w:style w:type="{kind}" w:styleId="{sid}"><w:name w:val="{name}"/>{b}'
                    f'{extra}<w:qFormat/>'
                    f'{"<w:pPr>" + ppr + "</w:pPr>" if ppr else ""}'
                    f'{"<w:rPr>" + rpr + "</w:rPr>" if rpr else ""}</w:style>')

        def heading(n, size, color, before, after):
            return style(f"Heading{n}", f"heading {n}",
                         ppr=f'<w:keepNext/><w:keepLines/><w:spacing w:before="{before}" '
                             f'w:after="{after}"/><w:outlineLvl w:val="{n - 1}"/>',
                         rpr=f'<w:b/><w:bCs/><w:color w:val="{color}"/>'
                             f'<w:sz w:val="{size}"/><w:szCs w:val="{size}"/>',
                         extra='<w:next w:val="Normal"/>')

        mono = f'<w:rFonts w:ascii="{MONO}" w:hAnsi="{MONO}" w:cs="{MONO}"/>'
        cb = 'w:val="single" w:sz="4" w:space="4" w:color="D0D7DE"'
        parts = [
            f'{XML_HEAD}<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">',
            '<w:docDefaults><w:rPrDefault><w:rPr>'
            f'<w:rFonts w:ascii="{FONT}" w:hAnsi="{FONT}" w:eastAsia="{FONT}" w:cs="{FONT}"/>'
            '<w:color w:val="262626"/><w:sz w:val="21"/><w:szCs w:val="21"/>'
            '<w:lang w:val="en-GB" w:eastAsia="en-US" w:bidi="ar-SA"/></w:rPr></w:rPrDefault>'
            '<w:pPrDefault><w:pPr><w:spacing w:after="120" w:line="264" w:lineRule="auto"/>'
            '</w:pPr></w:pPrDefault></w:docDefaults>',
            '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/>'
            '<w:qFormat/></w:style>',
            style("Title", "Title",
                  ppr=f'<w:pBdr><w:bottom w:val="single" w:sz="12" w:space="6" w:color="{ACCENT}"/>'
                      '</w:pBdr><w:spacing w:before="1200" w:after="360"/>',
                  rpr=f'<w:b/><w:color w:val="{ACCENT}"/><w:sz w:val="48"/><w:szCs w:val="48"/>'),
            heading(1, 32, ACCENT, 360, 120),
            heading(2, 26, ACCENT2, 240, 80),
            heading(3, 22, ACCENT2, 200, 60),
            style("TOCHeading", "TOC Heading", ppr='<w:spacing w:before="480" w:after="160"/>',
                  rpr=f'<w:b/><w:color w:val="{ACCENT}"/><w:sz w:val="28"/><w:szCs w:val="28"/>'),
            style("TOC1", "toc 1",
                  ppr=f'<w:tabs><w:tab w:val="right" w:leader="dot" w:pos="{TEXT_W}"/></w:tabs>'
                      '<w:spacing w:before="40" w:after="40"/>'),
            style("TOC2", "toc 2",
                  ppr=f'<w:tabs><w:tab w:val="right" w:leader="dot" w:pos="{TEXT_W}"/></w:tabs>'
                      '<w:spacing w:before="20" w:after="20"/><w:ind w:left="220"/>'),
            style("Label", "Label", ppr='<w:keepNext/><w:spacing w:before="180" w:after="60"/>',
                  rpr=f'<w:b/><w:color w:val="{ACCENT}"/>'),
            style("ListParagraph", "List Paragraph",
                  ppr='<w:spacing w:after="60"/><w:contextualSpacing/>'),
            style("TableText", "Table Text", ppr='<w:spacing w:before="0" w:after="0" w:line="252" '
                  'w:lineRule="auto"/>', rpr='<w:sz w:val="19"/><w:szCs w:val="19"/>'),
            style("Code", "Code",
                  ppr=f'<w:pBdr><w:top {cb}/><w:left {cb}/><w:bottom {cb}/><w:right {cb}/></w:pBdr>'
                      '<w:shd w:val="clear" w:color="auto" w:fill="F6F8FA"/>'
                      '<w:spacing w:before="0" w:after="0" w:line="240" w:lineRule="auto"/>'
                      '<w:ind w:left="120" w:right="120"/>',
                  rpr=mono + '<w:noProof/><w:color w:val="1F2328"/><w:sz w:val="17"/><w:szCs w:val="17"/>'),
            style("Quote", "Quote",
                  ppr=f'<w:pBdr><w:left w:val="single" w:sz="24" w:space="8" w:color="{ACCENT2}"/></w:pBdr>'
                      '<w:shd w:val="clear" w:color="auto" w:fill="EEF3FA"/>'
                      '<w:spacing w:before="160" w:after="200"/><w:ind w:left="240" w:right="240"/>',
                  rpr=f'<w:color w:val="{ACCENT}"/>'),
            style("Caption", "caption", ppr='<w:spacing w:before="0" w:after="240"/><w:jc w:val="center"/>',
                  rpr='<w:i/><w:iCs/><w:color w:val="595959"/><w:sz w:val="18"/><w:szCs w:val="18"/>'),
            style("Header", "header", rpr='<w:color w:val="7F7F7F"/><w:sz w:val="16"/><w:szCs w:val="16"/>'),
            style("Footer", "footer",
                  ppr=f'<w:pBdr><w:top w:val="single" w:sz="4" w:space="4" w:color="{BORDER}"/></w:pBdr>'
                      f'<w:tabs><w:tab w:val="right" w:pos="{TEXT_W}"/></w:tabs>',
                  rpr='<w:color w:val="7F7F7F"/><w:sz w:val="16"/><w:szCs w:val="16"/>'),
            style("InlineCode", "Inline Code", kind="character", based=None,
                  rpr=mono + '<w:color w:val="1F3864"/><w:sz w:val="18"/><w:szCs w:val="18"/>'
                      '<w:shd w:val="clear" w:color="auto" w:fill="EFF1F3"/>'),
            "</w:styles>",
        ]
        return "".join(parts)

    def write(self, out_path):
        self.convert()
        now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        exts = {Path(n).suffix.lstrip(".") for _, n, _ in self.media}
        ct_img = "".join(
            f'<Default Extension="{e}" ContentType="image/{"jpeg" if e == "jpg" else e}"/>'
            for e in sorted(exts))
        wml = "application/vnd.openxmlformats-officedocument.wordprocessingml"
        content_types = (
            f'{XML_HEAD}<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            f'<Default Extension="xml" ContentType="application/xml"/>{ct_img}'
            f'<Override PartName="/word/document.xml" ContentType="{wml}.document.main+xml"/>'
            f'<Override PartName="/word/styles.xml" ContentType="{wml}.styles+xml"/>'
            f'<Override PartName="/word/numbering.xml" ContentType="{wml}.numbering+xml"/>'
            f'<Override PartName="/word/settings.xml" ContentType="{wml}.settings+xml"/>'
            f'<Override PartName="/word/header1.xml" ContentType="{wml}.header+xml"/>'
            f'<Override PartName="/word/footer1.xml" ContentType="{wml}.footer+xml"/>'
            '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
            '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
            '</Types>')
        rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
        root_rels = (
            f'{XML_HEAD}<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Id="rId1" Type="{rel}/officeDocument" Target="word/document.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/'
            'metadata/core-properties" Target="docProps/core.xml"/>'
            f'<Relationship Id="rId3" Type="{rel}/extended-properties" Target="docProps/app.xml"/>'
            '</Relationships>')
        doc_rels = (
            f'{XML_HEAD}<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Id="rIdStyles" Type="{rel}/styles" Target="styles.xml"/>'
            f'<Relationship Id="rIdNumbering" Type="{rel}/numbering" Target="numbering.xml"/>'
            f'<Relationship Id="rIdSettings" Type="{rel}/settings" Target="settings.xml"/>'
            f'<Relationship Id="rIdHeader1" Type="{rel}/header" Target="header1.xml"/>'
            f'<Relationship Id="rIdFooter1" Type="{rel}/footer" Target="footer1.xml"/>'
            + "".join(f'<Relationship Id="{rid}" Type="{rel}/image" Target="{n}"/>'
                      for rid, n, _ in self.media)
            + "</Relationships>")
        settings = (
            f'{XML_HEAD}<w:settings xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            '<w:defaultTabStop w:val="720"/><w:characterSpacingControl w:val="doNotCompress"/>'
            '<w:updateFields w:val="true"/>'
            '<w:compat><w:compatSetting w:name="compatibilityMode" '
            'w:uri="http://schemas.microsoft.com/office/word" w:val="15"/></w:compat></w:settings>')
        core = (
            f'{XML_HEAD}<cp:coreProperties '
            'xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
            f'<dc:title>{escape(self.title)}</dc:title>'
            f'<dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created>'
            f'<dcterms:modified xsi:type="dcterms:W3CDTF">{now}</dcterms:modified>'
            '</cp:coreProperties>')
        app = (f'{XML_HEAD}<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/'
               'extended-properties"><Application>build_docx.py</Application></Properties>')

        document = self.document_xml()  # also fills title/version for header/footer
        with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("[Content_Types].xml", content_types)
            z.writestr("_rels/.rels", root_rels)
            z.writestr("word/_rels/document.xml.rels", doc_rels)
            z.writestr("word/document.xml", document)
            z.writestr("word/styles.xml", self.styles_xml())
            z.writestr("word/numbering.xml", self.numbering_xml())
            z.writestr("word/settings.xml", settings)
            z.writestr("word/header1.xml", self.header_xml())
            z.writestr("word/footer1.xml", self.footer_xml())
            z.writestr("docProps/core.xml", core)
            z.writestr("docProps/app.xml", app)
            for _, name, data in self.media:
                z.writestr(f"word/{name}", data)
        return out_path


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    src = Path(sys.argv[1])
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else src.with_suffix(".docx")
    Converter(src).write(out)
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
