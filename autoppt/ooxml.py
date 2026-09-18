"""只读取Office文件结构，保留文本换行、表格行和图片来源。"""

from __future__ import annotations

import base64
import io
import posixpath
import re
import zipfile
from pathlib import Path

from lxml import etree as ET
from PIL import Image, ImageOps

NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
    "ct": "http://schemas.openxmlformats.org/package/2006/content-types",
}
MAX_FILE = 200 * 1024 * 1024
MAX_EXPANDED = 700 * 1024 * 1024
PARSER = ET.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)


def tag(prefix, name):
    return f"{{{NS[prefix]}}}{name}"


def xml(data):
    return ET.fromstring(data, parser=PARSER)


def encoded(node):
    return ET.tostring(node, xml_declaration=True, encoding="UTF-8", standalone=True)


def compact(value):
    return re.sub(r"\s+", "", value or "")


def rel_path(part):
    parent, name = posixpath.split(part)
    return posixpath.join(parent, "_rels", name + ".rels")


def resolve(part, target):
    if target.startswith("/"):
        return target.lstrip("/")
    return posixpath.normpath(posixpath.join(posixpath.dirname(part), target))


def read_package(path):
    path = Path(path)
    if path.stat().st_size > MAX_FILE:
        raise ValueError(f"文件超过200MB限制：{path.name}")
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        if len(entries) > 12000 or sum(e.file_size for e in entries) > MAX_EXPANDED:
            raise ValueError(f"文件解压规模超过限制：{path.name}")
        return {e.filename: archive.read(e) for e in entries if not e.is_dir()}


def relationships(package, part):
    data = package.get(rel_path(part))
    if not data:
        return {}
    return {n.get("Id"): dict(n.attrib) for n in xml(data)}


def paragraphs(node, prefix="a"):
    """保留段内换行和制表符，防止多个任职条目被拼接。"""
    result = []
    for para in node.findall(f".//{tag(prefix, 'p')}"):
        tokens = []
        for child in para.iter():
            if child.tag == tag(prefix, "t"):
                tokens.append(child.text or "")
            elif child.tag == tag(prefix, "br"):
                tokens.append("\n")
            elif child.tag == tag(prefix, "tab"):
                tokens.append("\t")
        result.extend(line.strip() for line in "".join(tokens).split("\n") if line.strip())
    return result


def geometry(node, parent_transform=(0, 0, 1, 1)):
    xfrm = node.find("a:xfrm", NS)
    if xfrm is None:
        xfrm = node.find("p:spPr/a:xfrm", NS)
    if xfrm is None:
        xfrm = node.find("p:xfrm", NS)
    if xfrm is None:
        return [0, 0, 0, 0]
    off, ext = xfrm.find("a:off", NS), xfrm.find("a:ext", NS)
    if off is None or ext is None:
        return [0, 0, 0, 0]
    tx, ty, sx, sy = parent_transform
    return [tx + int(off.get("x", 0)) * sx, ty + int(off.get("y", 0)) * sy,
            int(ext.get("cx", 0)) * sx, int(ext.get("cy", 0)) * sy]


def image_thumbnail(data, size=(260, 210)):
    try:
        with Image.open(io.BytesIO(data)) as im:
            im = ImageOps.exif_transpose(im)
            im.thumbnail(size)
            if im.mode not in ("RGB", "RGBA"):
                im = im.convert("RGB")
            buffer = io.BytesIO()
            im.save(buffer, "PNG")
            return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()
    except Exception:
        return ""


def slide_shapes(root, package, part):
    rels = relationships(package, part)
    shapes = []

    def walk(container, transform=(0, 0, 1, 1)):
        for node in container:
            kind = ET.QName(node).localname
            if kind == "grpSp":
                xf = node.find("p:grpSpPr/a:xfrm", NS)
                if xf is not None:
                    off, ext = xf.find("a:off", NS), xf.find("a:ext", NS)
                    co, ce = xf.find("a:chOff", NS), xf.find("a:chExt", NS)
                    if all(n is not None for n in (off, ext, co, ce)):
                        tx, ty, sx, sy = transform
                        xs = int(ext.get("cx")) / max(1, int(ce.get("cx")))
                        ys = int(ext.get("cy")) / max(1, int(ce.get("cy")))
                        local = (tx + sx * (int(off.get("x")) - xs * int(co.get("x"))),
                                 ty + sy * (int(off.get("y")) - ys * int(co.get("y"))), sx * xs, sy * ys)
                        walk(node, local)
                        continue
                walk(node, transform)
                continue
            if kind not in ("sp", "pic", "graphicFrame"):
                continue
            prop = node.find(".//p:cNvPr", NS)
            if prop is None:
                continue
            lines = paragraphs(node)
            bbox = geometry(node, transform)
            fonts = [int(v) / 100 for v in node.xpath(".//a:rPr/@sz | .//a:defRPr/@sz", namespaces=NS)]
            shape = {"id": prop.get("id"), "name": prop.get("name", ""), "kind": kind,
                     "lines": lines, "text": "\n".join(lines), "bbox": bbox,
                     "font": max(fonts, default=18), "table": []}
            for row in node.findall(".//a:tr", NS):
                shape["table"].append([" ".join(paragraphs(cell)) for cell in row.findall("a:tc", NS)])
            blip = node.find(".//a:blip", NS)
            if kind == "pic" and blip is not None:
                rid = blip.get(tag("r", "embed"))
                rel = rels.get(rid, {})
                target = resolve(part, rel.get("Target", ""))
                shape.update(image=target, rid=rid, thumbnail=image_thumbnail(package.get(target, b"")))
            shapes.append(shape)

    tree = root.find("p:cSld/p:spTree", NS)
    if tree is not None:
        walk(tree)
    return shapes


def read_deck(path):
    package = read_package(path)
    root = xml(package["ppt/presentation.xml"])
    size = root.find("p:sldSz", NS)
    width, height = int(size.get("cx")), int(size.get("cy"))
    rels = relationships(package, "ppt/presentation.xml")
    slides = []
    # 读取实际播放顺序，不能按slide文件编号排序。
    for number, ref in enumerate(root.findall("p:sldIdLst/p:sldId", NS), 1):
        rel = rels[ref.get(tag("r", "id"))]
        part = resolve("ppt/presentation.xml", rel["Target"])
        slide_root = xml(package[part])
        shapes = slide_shapes(slide_root, package, part)
        slides.append({"number": number, "part": part, "shapes": shapes,
                       "text": "\n".join(s["text"] for s in shapes),
                       "hidden": slide_root.get("show") == "0"})
    return {"path": str(Path(path).resolve()), "width": width, "height": height, "slides": slides}


def read_docx(path):
    package = read_package(path)
    root = xml(package["word/document.xml"])
    lines = []
    body = root.find("w:body", NS)
    for node in body:
        if node.tag == tag("w", "tbl"):
            for row in node.findall("w:tr", NS):
                cells = [" ".join(paragraphs(c, "w")) for c in row.findall("w:tc", NS)]
                if any(cells):
                    lines.append(" ".join(c for c in cells if c))
        else:
            lines.extend(paragraphs(node, "w") if node.tag != tag("w", "p")
                         else paragraphs(ET.fromstring(b"<root>" + ET.tostring(node) + b"</root>"), "w"))
    rels = relationships(package, "word/document.xml")
    images = []
    for blip in root.findall(".//a:blip", NS):
        rel = rels.get(blip.get(tag("r", "embed")), {})
        target = resolve("word/document.xml", rel.get("Target", ""))
        if target in package:
            thumb = image_thumbnail(package[target])
            if thumb:
                images.append({"image": target, "thumbnail": thumb})
    return lines, images


def unpack_experts(path, destination):
    """限制解压范围及总大小，压缩包中的Office文件均以安全文件名保存。"""
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    result = []
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        if len(entries) > 1000 or sum(e.file_size for e in entries) > MAX_EXPANDED:
            raise ValueError("压缩包解压规模超过限制")
        for index, entry in enumerate(entries):
            if entry.is_dir():
                continue
            name = entry.filename
            if not entry.flag_bits & 0x800:
                try:
                    name = name.encode("cp437").decode("gbk")
                except UnicodeError:
                    pass
            normalized = name.replace("\\", "/")
            if normalized.startswith("/") or ".." in normalized.split("/") or ":" in normalized:
                raise ValueError("压缩包包含不安全路径")
            leaf = Path(normalized).name
            if leaf.startswith("~$") or Path(leaf).suffix.lower() not in (".pptx", ".docx"):
                continue
            target = destination / f"{index:03d}_{leaf}"
            target.write_bytes(archive.read(entry))
            result.append(target)
    return result
