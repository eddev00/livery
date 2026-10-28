"""SmartArt diagrams -> pictures.

pandoc only extracts real pictures from a .docx; a SmartArt diagram is a
DrawingML `dgm` graphic and is silently dropped. LibreOffice can render them,
so each diagram is rasterised on its own (temporary one-paragraph document ->
PDF -> PNG, trimmed to its content) and the diagram in the Word file is replaced
by that picture before pandoc runs. The diagram's text is collected for the
report, because text inside an image cannot be verified.
"""
from __future__ import annotations

import html
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .tools import find_tool, run

_DRAWING_RE = re.compile(r"<w:drawing>.*?</w:drawing>", re.S)
_DOC_OPEN_RE = re.compile(r"<w:document[^>]*>", re.S)
_SECT_RE = re.compile(r"<w:sectPr[ >].*?</w:sectPr>\s*</w:body>", re.S)
_IMAGE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"


@dataclass
class SmartArt:
    index: int
    png: Path
    text: list[str] = field(default_factory=list)


def has_smartart(docx: Path) -> bool:
    with zipfile.ZipFile(docx) as z:
        return b"<dgm:relIds" in z.read("word/document.xml")


def _diagram_text(z: zipfile.ZipFile, drawing_xml: str, rels_xml: str) -> list[str]:
    m = re.search(r'r:dm="([^"]+)"', drawing_xml)
    if not m:
        return []
    rel = re.search(rf'<Relationship [^>]*Id="{re.escape(m.group(1))}"[^>]*Target="([^"]+)"', rels_xml)
    if not rel:
        return []
    try:
        data = z.read("word/" + rel.group(1)).decode("utf-8", errors="replace")
    except KeyError:
        return []
    # one entry per text body; runs are joined
    out = []
    for body in re.findall(r"<dgm:t>(.*?)</dgm:t>", data, re.S):
        t = html.unescape("".join(re.findall(r"<a:t>([^<]*)</a:t>", body))).strip()
        if t:
            out.append(t)
    return out


def _render(zip_src: Path, doc_open: str, sect: str, drawing_xml: str, work: Path, name: str) -> Path | None:
    """Render one drawing through LibreOffice into a trimmed PNG."""
    soffice = find_tool("soffice") or find_tool("libreoffice")
    pdftoppm = find_tool("pdftoppm")
    if soffice is None or pdftoppm is None:
        return None
    tmp_docx = work / f"{name}.docx"
    body = f"{doc_open}<w:body><w:p><w:r>{drawing_xml}</w:r></w:p>{sect}</w:document>"
    with zipfile.ZipFile(zip_src) as zin, zipfile.ZipFile(tmp_docx, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            if item.filename == "word/document.xml":
                zout.writestr(item, body.encode("utf-8"))
            else:
                zout.writestr(item, zin.read(item.filename))
    profile = work / "lo-profile"
    profile.mkdir(exist_ok=True)
    run([soffice, f"-env:UserInstallation={profile.resolve().as_uri()}", "--headless",
         "--convert-to", "pdf", "--outdir", work, tmp_docx], check=False)
    pdf = work / f"{name}.pdf"
    if not pdf.exists():
        return None
    run([pdftoppm, "-png", "-r", "220", "-f", "1", "-l", "1", "-singlefile", pdf, work / name])
    raw = work / f"{name}.png"
    if not raw.exists():
        return None
    from PIL import Image, ImageChops

    im = Image.open(raw).convert("RGB")
    bg = Image.new("RGB", im.size, (255, 255, 255))
    bbox = ImageChops.difference(im, bg).getbbox()
    if bbox:
        pad = 12
        bbox = (max(0, bbox[0] - pad), max(0, bbox[1] - pad), min(im.width, bbox[2] + pad), min(im.height, bbox[3] + pad))
        im = im.crop(bbox)
    out = work / f"{name}-trimmed.png"
    im.save(out)
    return out


def _picture_xml(rid: str, cx: int, cy: int, docpr_id: str, name: str) -> str:
    return (
        '<w:drawing><wp:inline distT="0" distB="0" distL="0" distR="0">'
        f'<wp:extent cx="{cx}" cy="{cy}"/><wp:docPr id="{docpr_id}" name="{name}"/>'
        '<a:graphic xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        '<a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
        '<pic:pic xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">'
        f'<pic:nvPicPr><pic:cNvPr id="0" name="{name}.png"/><pic:cNvPicPr/></pic:nvPicPr>'
        f'<pic:blipFill><a:blip r:embed="{rid}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>'
        f'<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr></pic:pic></a:graphicData></a:graphic>'
        '</wp:inline></w:drawing>'
    )


def replace_smartart(docx_in: Path, docx_out: Path, work: Path) -> list[SmartArt]:
    """Write docx_out = docx_in with every SmartArt diagram replaced by a rendered picture."""
    work.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(docx_in) as z:
        doc = z.read("word/document.xml").decode("utf-8")
        rels = z.read("word/_rels/document.xml.rels").decode("utf-8")
        ctypes = z.read("[Content_Types].xml").decode("utf-8")
        doc_open = _DOC_OPEN_RE.search(doc).group(0)
        sect_m = _SECT_RE.search(doc)
        sect = sect_m.group(0) if sect_m else "</w:body>"
        # no running header/footer in the temporary render document
        sect = re.sub(r"<w:(header|footer)Reference [^>]*/>", "", sect)
        results: list[SmartArt] = []
        new_media: dict[str, bytes] = {}
        counter = 0

        def substitute(m: re.Match) -> str:
            nonlocal counter, rels
            drawing = m.group(0)
            if "<dgm:relIds" not in drawing:
                return drawing
            counter += 1
            name = f"wordeasy-smartart-{counter}"
            png = _render(docx_in, doc_open, sect, drawing, work, name)
            text = _diagram_text(z, drawing, rels)
            if png is None:
                results.append(SmartArt(counter, Path(""), text))
                return drawing
            from PIL import Image

            w, h = Image.open(png).size
            ext = re.search(r'<wp:extent cx="(\d+)" cy="(\d+)"/>', drawing)
            cx = int(ext.group(1)) if ext else 5000000
            cy = int(cx * h / w)
            docpr = re.search(r'<wp:docPr id="(\d+)"', drawing)
            rid = f"rIdWordeasy{counter}"
            rels = rels.replace("</Relationships>",
                                f'<Relationship Id="{rid}" Type="{_IMAGE_REL}" Target="media/{name}.png"/></Relationships>')
            new_media[f"word/media/{name}.png"] = png.read_bytes()
            results.append(SmartArt(counter, png, text))
            return _picture_xml(rid, cx, cy, docpr.group(1) if docpr else "9000", name)

        new_doc = _DRAWING_RE.sub(substitute, doc)
        if 'Extension="png"' not in ctypes:
            ctypes = ctypes.replace("<Default ", '<Default Extension="png" ContentType="image/png"/><Default ', 1)
        with zipfile.ZipFile(docx_out, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in z.infolist():
                if item.filename == "word/document.xml":
                    zout.writestr(item, new_doc.encode("utf-8"))
                elif item.filename == "word/_rels/document.xml.rels":
                    zout.writestr(item, rels.encode("utf-8"))
                elif item.filename == "[Content_Types].xml":
                    zout.writestr(item, ctypes.encode("utf-8"))
                else:
                    zout.writestr(item, z.read(item.filename))
            for path, data in new_media.items():
                zout.writestr(path, data)
    return results
