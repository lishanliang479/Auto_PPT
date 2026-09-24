"""复制模板原始页面并定点填充，不重建母版、背景或装饰。"""

from __future__ import annotations

import copy
import io
import json
import math
import os
import posixpath
import re
import time
import zipfile
from functools import lru_cache
from pathlib import Path

from lxml import etree as ET
from PIL import Image, ImageFont, ImageOps

from .analyze import DATE, EVENT_ROLES, PERSON_ROLES, ROLES, ROLE_TEXTS, infer_slide_fields, issue, names_in
from .bio import summarize_bio
from .ooxml import NS, compact, encoded, paragraphs, read_deck, read_package, rel_path, resolve, tag, xml

ROLE_LABELS = {"chair": "大会主席", "host": "大会主持", "speaker": "大会讲者", "guest": "讨论嘉宾"}


def display_name(name):
    """两个汉字的姓名在展示时加入一个空格，内部匹配仍使用原姓名。"""
    value = str(name or "").strip()
    if len(value) == 2 and all("\u4e00" <= char <= "\u9fff" for char in value):
        return value[0] + " " + value[1]
    return value


def balanced_lines(value, count):
    """按模板原有段落数量均衡拆分标题，保留后续日期段落的独立格式。"""
    text = str(value or "")
    if count <= 1:
        return [text]
    if len(text) < count * 6:
        return [text] + [""] * (count - 1)
    result = []
    for index in range(count):
        start = round(len(text) * index / count)
        end = round(len(text) * (index + 1) / count)
        result.append(text[start:end])
    return result


def find_shape(root, shape_id):
    for prop in root.findall(".//p:cNvPr", NS):
        if prop.get("id") == str(shape_id):
            return prop.getparent().getparent()
    raise ValueError(f"模板填充区域不存在：{shape_id}")


def shape_info(slide, shape_id):
    return next(s for s in slide["shapes"] if s["id"] == str(shape_id))


def photo_frame_shape(slide, photo):
    """查找照片后方的可见框，优先匹配横向位置、宽度和底边一致的空白形状。"""
    px, py, pw, ph = photo["bbox"]
    if pw <= 0 or ph <= 0:
        return None
    candidates = []
    for shape in slide["shapes"]:
        if shape["id"] == photo["id"] or shape["kind"] != "sp" or shape["text"]:
            continue
        x, y, width, height = shape["bbox"]
        if width <= 0 or height <= 0 or not .55 <= height / ph <= 1.6:
            continue
        dx = abs(x - px) / pw
        dw = abs(width - pw) / pw
        db = abs((y + height) - (py + ph)) / ph
        if dx <= .14 and dw <= .14 and db <= .14:
            rounded = shape.get("geometry") in ("roundRect", "round1Rect", "round2SameRect", "round2DiagRect")
            # 圆角矩形优先，其次按横向位置、宽度和底边的接近程度选择。
            candidates.append((dx + dw + db - (.2 if rounded else 0), shape))
    return min(candidates, key=lambda item: item[0])[1] if candidates else None


def align_shape_bbox(node, current_bbox, target_bbox):
    """按页面坐标调整对象边界，兼容位于普通组合中的图片对象。"""
    transform = node.find("a:xfrm", NS)
    if transform is None:
        transform = node.find("p:spPr/a:xfrm", NS)
    if transform is None:
        return
    offset, extent = transform.find("a:off", NS), transform.find("a:ext", NS)
    if offset is None or extent is None:
        return
    current_x, current_y, current_width, current_height = current_bbox
    target_x, target_y, target_width, target_height = target_bbox
    local_width, local_height = int(extent.get("cx", 0)), int(extent.get("cy", 0))
    if current_width <= 0 or current_height <= 0 or local_width <= 0 or local_height <= 0:
        return
    scale_x, scale_y = current_width / local_width, current_height / local_height
    offset.set("x", str(round(int(offset.get("x", 0)) + (target_x - current_x) / scale_x)))
    offset.set("y", str(round(int(offset.get("y", 0)) + (target_y - current_y) / scale_y)))
    extent.set("cx", str(round(target_width / scale_x)))
    extent.set("cy", str(round(target_height / scale_y)))


def clear_links(node):
    # 原文本上的旧网址、动作和字段不能跟随新专家一起复制。
    for item in list(node.iter()):
        if ET.QName(item).localname in ("hlinkClick", "hlinkMouseOver"):
            item.getparent().remove(item)


def bring_to_front(node):
    """将对象移到当前组合的顶层，同时保持扩展节点位于结构末尾。"""
    parent = node.getparent()
    parent.remove(node)
    extension = parent.find("p:extLst", NS)
    if extension is None:
        parent.append(node)
    else:
        parent.insert(parent.index(extension), node)


def template_run_properties(paragraph, body):
    """合并模板文字片段与段落默认样式，得到新文字应使用的完整格式。"""
    candidates = paragraph.xpath('./a:r | ./a:fld', namespaces=NS)
    selected = next((run for run in candidates
                     if ''.join(run.xpath('./a:t/text()', namespaces=NS)).strip()), None)
    primary = selected.find('a:rPr', NS) if selected is not None else paragraph.find('a:endParaRPr', NS)
    result = copy.deepcopy(primary) if primary is not None else ET.Element(tag("a", "rPr"))
    ppr = paragraph.find('a:pPr', NS)
    level = int(ppr.get('lvl', '0')) + 1 if ppr is not None else 1
    defaults = []
    if ppr is not None:
        defaults.append(ppr.find('a:defRPr', NS))
    defaults.extend(body.xpath(f'./a:lstStyle/a:lvl{level}pPr/a:defRPr | ./a:lstStyle/a:defPPr/a:defRPr',
                               namespaces=NS))
    # 文字片段的显式格式优先，缺少的字体、字号及颜色从最近的模板默认样式补齐。
    for fallback in defaults:
        if fallback is None:
            continue
        for key, value in fallback.attrib.items():
            if key not in result.attrib:
                result.set(key, value)
        existing = {child.tag for child in result}
        for child in fallback:
            if child.tag not in existing and ET.QName(child).localname not in ('hlinkClick', 'hlinkMouseOver'):
                result.append(copy.deepcopy(child))
                existing.add(child.tag)
    clear_links(result)
    return result


def set_text(node, lines, *, font=None, preserve_sizes=True, line_spacing=None):
    metadata = node.find('.//p:cNvPr', NS)
    if metadata is not None:
        metadata.attrib.pop('descr', None)
        metadata.attrib.pop('title', None)
    body = node.find("p:txBody", NS)
    if body is None:
        raise ValueError("选择的文本区域不支持直接编辑，请选择普通文本框")
    source = body.findall("a:p", NS)
    templates = ([copy.deepcopy(p) for p in source if p.xpath('.//a:t/text()', namespaces=NS)]
                 or [copy.deepcopy(p) for p in source]
                 or [ET.Element(tag("a", "p"))])
    for para in source:
        body.remove(para)
    for index, value in enumerate(lines or [""]):
        original = templates[min(index, len(templates) - 1)]
        p = ET.SubElement(body, tag("a", "p"))
        ppr = original.find("a:pPr", NS)
        if ppr is not None:
            p.append(copy.deepcopy(ppr))
        if line_spacing is not None:
            # 专家简介固定字号后，通过统一行距保证十二行仍落在模板文本框内。
            ppr = p.find("a:pPr", NS)
            if ppr is None:
                ppr = ET.Element(tag("a", "pPr"))
                p.insert(0, ppr)
            spacing = ppr.find("a:lnSpc", NS)
            if spacing is None:
                spacing = ET.SubElement(ppr, tag("a", "lnSpc"))
            for child in list(spacing):
                spacing.remove(child)
            ET.SubElement(spacing, tag("a", "spcPct"), val=str(round(line_spacing * 100000)))
        run = ET.SubElement(p, tag("a", "r"))
        # 空白占位符常带有异常字号，只使用实际文字及其所在段落的模板格式。
        rpr = template_run_properties(original, body)
        if font is not None:
            rpr.set("sz", str(round(font * 100)))
        run.append(rpr)
        t = ET.SubElement(run, tag("a", "t"))
        t.text = str(value)
        t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        end = original.find("a:endParaRPr", NS)
        if end is not None:
            p.append(copy.deepcopy(end))


@lru_cache(maxsize=64)
def measure_font(size, family=""):
    windir = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    choices = (["simsun.ttc", "msyh.ttc"] if "宋" in family or "SimSun" in family else
               ["simhei.ttf", "msyh.ttc", "simsun.ttc"] if "黑" in family else ["msyh.ttc", "simsun.ttc"])
    for path in [*(windir / n for n in choices), Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")]:
        if path.exists():
            return ImageFont.truetype(str(path), max(1, round(size * 2)))
    return ImageFont.load_default()


def text_width(value, font):
    return font.getlength(value) / 2


def text_box(node, shape, font_size=None):
    body = node.find("p:txBody/a:bodyPr", NS)
    text_body = node.find("p:txBody", NS)
    width = shape["bbox"][2] / 12700
    height = shape["bbox"][3] / 12700
    if body is not None:
        width -= (int(body.get("lIns", "91440")) + int(body.get("rIns", "91440"))) / 12700
        height -= (int(body.get("tIns", "45720")) + int(body.get("bIns", "45720"))) / 12700
    template_paragraph = next((paragraph for paragraph in node.findall('p:txBody/a:p', NS)
                               if ''.join(paragraph.xpath('.//a:t/text()', namespaces=NS)).strip()), None)
    style = template_run_properties(template_paragraph, text_body) if template_paragraph is not None else None
    size = font_size or (int(style.get('sz')) / 100 if style is not None and style.get('sz') else shape["font"])
    families = (style.xpath("./a:ea/@typeface | ./a:latin/@typeface", namespaces=NS)
                if style is not None else [])
    if not families:
        families = node.xpath(".//a:rPr/a:ea/@typeface | .//a:rPr/a:latin/@typeface", namespaces=NS)
    family = families[0] if families else ""
    # 留出字体替换与Office排版差异的余量，估算通过仍需实际预览。
    return max(10, width * .95), max(10, height * .93), size, family


def wrap_text(value, width, font):
    if not value:
        return [""]
    result, line = [], ""
    for ch in value:
        if line and text_width(line + ch, font) > width:
            result.append(line)
            line = ch
        else:
            line += ch
    if line:
        result.append(line)
    return result


def estimated_height(lines, width, size, family, node, line_spacing=None):
    font = measure_font(size, family)
    count = sum(len(wrap_text(line, width, font)) for line in lines)
    spacing = line_spacing or max([int(v) / 1000 for v in node.xpath(".//a:lnSpc/a:spcPct/@val", namespaces=NS)] or [120]) / 100
    absolute = max([int(v) / 100 for v in node.xpath(".//a:lnSpc/a:spcPts/@val", namespaces=NS)] or [0])
    before_after = sum(int(v) / 100 for v in node.xpath("./p:txBody/a:p[1]/a:pPr/a:spcBef/a:spcPts/@val | ./p:txBody/a:p[1]/a:pPr/a:spcAft/a:spcPts/@val", namespaces=NS))
    # 百分比行距基于字体的自然行高，中文字体自然行高通常大于字号。
    return count * max(size * 1.22 * max(1.0, spacing), absolute) + len(lines) * before_after


def fit_lines(node, shape, lines, minimum=14):
    width, height, original_size, family = text_box(node, shape)
    if compact(''.join(paragraphs(node))) == compact(''.join(lines)):
        return False, original_size
    size = original_size
    lower_bound = min(original_size, minimum)
    font = measure_font(size, family)
    needs_wrap = any(text_width(line, font) > width for line in lines)
    # 优先沿用模板字号，发生溢出后才逐级缩小，并保持在模板文本框内换行。
    while size > lower_bound and estimated_height(lines, width, size, family, node) > height:
        size = max(lower_bound, size - .5)
    overflow = estimated_height(lines, width, size, family, node) > height
    set_text(node, lines, font=size if size < original_size else None)
    if needs_wrap or size < original_size:
        body = node.find("p:txBody/a:bodyPr", NS)
        if body is not None:
            body.set("wrap", "square")
    return overflow, size


def fit_meeting_title(node, shape, value, single_line_minimum=14, minimum=8):
    """会议名称优先保持单行，过长时在模板文本框内自动换行并缩放。"""
    width, height, original_size, family = text_box(node, shape)
    text = str(value or "")
    measured = text_width(text, measure_font(original_size, family))
    single_line_size = original_size
    if measured > width and measured > 0:
        # 按可用宽度等比缩小，并留出Office字体替换造成的排版余量。
        single_line_size = original_size * width / measured * .96
    body = node.find("p:txBody/a:bodyPr", NS)
    if single_line_size >= single_line_minimum:
        size = min(original_size, single_line_size)
        overflow = text_width(text, measure_font(size, family)) > width
        if body is not None:
            body.set("wrap", "none")
    else:
        # 单行字号过小时恢复换行，并逐级缩小到宽度和高度都落在模板框内。
        size = original_size
        while size > minimum and estimated_height([text], width, size, family, node) > height:
            size = max(minimum, size - .25)
        overflow = estimated_height([text], width, size, family, node) > height
        if body is not None:
            body.set("wrap", "square")
    set_text(node, [text], font=size if size < original_size else None)
    return overflow, size


def select_single_page_bio(node, shape, person):
    """使用模板原有字号和行距放置简介，容量不足时精简低优先级内容。"""
    width, height, size, family = text_box(node, shape)
    summary = summarize_bio(person)
    lines = summary['lines'][:]
    # 长研究方向需要自动换行时，先减少低优先级社会任职，保留个人专业介绍。
    while len(lines) > 1 and estimated_height(lines, width, size, family, node) > height:
        social_indexes = [index for index, value in enumerate(lines) if value in summary['social']]
        other_selected = any(value in summary['other'] for value in lines)
        if other_selected and social_indexes:
            lines.pop(social_indexes[-1])
        else:
            lines.pop()
    adjusted = lines != summary['lines']
    # 单行仍超出模板容量时仅精简文字，保持模板格式不变。
    while lines and estimated_height(lines, width, size, family, node) > height:
        if len(lines) > 1:
            lines.pop()
        elif len(lines[0]) > 12:
            lines[0] = lines[0][:-4].rstrip("，,；;。 ") + "…"
        else:
            lines = []
        adjusted = True
    selected_social = [x for x in lines if x in summary['social']]
    summary.update(lines=lines, font=size, line_spacing="template", fit_adjusted=adjusted,
                   selected_social=selected_social,
                   omitted_social=[x for x in summary['social'] if x not in selected_social])
    return summary


def validate_model(model):
    warnings = []
    agenda, profile = model["agenda"], model["template"]
    if not agenda.get("title"):
        warnings.append(issue("meeting_title_empty", "会议名称为空，对应模板区域将留空"))
    if not agenda.get("date"):
        warnings.append(issue("meeting_date_empty", "会议日期为空，对应模板区域将留空"))
    if not agenda.get("events"):
        warnings.append(issue("agenda_empty", "日程没有可生成的环节，本次仅生成模板封面和结束页"))
    experts = {e.get("name", ""): e for e in model["experts"] if e.get("name")}
    if len(experts) != len([e for e in model["experts"] if e.get("name")]):
        warnings.append(issue("duplicate_name", "存在同名专家资料，生成时使用最后一份资料"))
    if any(not e.get("name") for e in model["experts"]):
        warnings.append(issue("expert_name_empty", "存在姓名为空的专家资料，生成时跳过无法匹配的资料"))
    required = set()
    previous_end = None
    for event in agenda["events"]:
        required.update(name for name in event.get("people", []) if name)
        required.update(name for name in event.get("hosts", []) if name)
        if event.get("kind") not in ("opening", "talk", "discussion", "summary"):
            warnings.append(issue("event_kind", f"{event.get('time', '')}的环节类型无法识别，按专题讲题生成"))
        if not event.get("people") or not event.get("title"):
            warnings.append(issue("event_incomplete", f"{event.get('time', '')}缺少人员或内容，对应区域将留空"))
        times = re.findall(r"(\d{1,2}):([0-5]\d)", event.get("time", ""))
        if len(times) != 2 or any(int(h) > 23 for h, _ in times):
            if event.get("time"):
                warnings.append(issue("event_time", f"时间格式需要核对：{event.get('time', '')}"))
        else:
            start, end = [int(h) * 60 + int(m) for h, m in times]
            if end <= start or (previous_end is not None and start < previous_end):
                warnings.append(issue("event_time_order", f"日程时间重叠或顺序需要核对：{event['time']}"))
            previous_end = end
    missing = sorted(required - set(experts))
    if missing:
        warnings.append(issue("missing_expert", "缺少专家简介：" + "、".join(missing) + "，对应简介页使用待补充内容"))
    for name in required & set(experts):
        expert = experts[name]
        if not expert.get('photo_confirmed', True):
            warnings.append(issue("photo_unconfirmed", f"{name}有多张候选照片，生成时使用当前首选照片", expert=name))
        if not expert.get("bio"):
            warnings.append(issue("bio_empty", f"{name}简介为空，对应简介区域使用待补充内容", expert=name))
        if expert.get("photo") and expert["photo"] not in [p["image"] for p in expert["photos"]]:
            warnings.append(issue("photo_invalid", f"{name}选择的照片不存在，对应照片区域将留空", expert=name))
    for slide in profile["slides"]:
        if slide["role"] == "unknown":
            continue
        shapes = {s["id"]: s for s in slide["shapes"]}
        assigned = []
        for field, values in slide["fields"].items():
            for sid in values if isinstance(values, list) else [values]:
                if not sid:
                    continue
                if sid not in shapes:
                    warnings.append(issue("template_field_invalid", f"模板第{slide['number']}页区域不存在，生成时重新识别", slide=slide["number"]))
                elif field in ("photo", "people_photos") and not shapes[sid].get("image"):
                    warnings.append(issue("template_photo_invalid", f"模板第{slide['number']}页照片区域类型无效，生成时保留空白", slide=slide["number"]))
                elif field not in ("photo", "people_photos") and shapes[sid]["kind"] != "sp":
                    warnings.append(issue("template_text_invalid", f"模板第{slide['number']}页文字区域无法编辑，生成时保留原布局", slide=slide["number"]))
                if field not in ("meeting", "metadata"):
                    assigned.append(sid)
        if len(set(assigned)) != len(assigned):
            warnings.append(issue("template_field_duplicate", f"模板第{slide['number']}页多个字段使用同一区域，生成时重新识别", slide=slide["number"]))
    return missing, warnings


def page_plan(model):
    slides = model["template"]["slides"]
    plan = []
    deck_area = model["template"].get("width", 0) * model["template"].get("height", 0)

    if not slides:
        raise ValueError("模板没有可用页面")

    def add(role, **data):
        required = (["bio", "identity", "photo"] if role in PERSON_ROLES else
                    ["people", "title"] if role in EVENT_ROLES else
                    ["title"] if role == "cover" else [])
        visible = [slide for slide in slides if not slide.get("hidden")] or slides
        ranked = []
        for slide in visible:
            enriched = {**slide, "deck_area": deck_area, "deck_height": model["template"].get("height", 0)}
            fields = infer_slide_fields(enriched, role, existing=slide.get("fields"))
            complete = sum(bool(fields.get(field)) for field in required)
            same_group = ((role in PERSON_ROLES and slide["role"] in PERSON_ROLES)
                          or (role in EVENT_ROLES and slide["role"] in EVENT_ROLES))
            # 完整填充区域优先于角色名称完全相同，避免选中缺少人员或照片的空布局。
            score = complete * 50 + (45 if slide["role"] == role else 0) + (25 if same_group else 0)
            if role == "cover":
                score += max(0, 20 - slide["number"])
            elif role == "ending":
                score += slide["number"]
            ranked.append((score, slide, fields))
        _, chosen, fields = max(ranked, key=lambda item: item[0])
        missing_fields = [field for field in required if not fields.get(field)]
        plan.append({"role": role, "template_slide": chosen["number"], "fields": fields,
                     "template_fallback": chosen["role"] != role, "missing_fields": missing_fields, **data})

    add("cover")
    last_hosts = None
    for event in model["agenda"]["events"]:
        role = event["kind"] if event.get("kind") in EVENT_ROLES else "talk"
        if role in ("talk", "discussion") and event.get("hosts") and event["hosts"] != last_hosts:
            for name in (name for name in event["hosts"] if name):
                add("host", expert=name, time=event["time"])
            last_hosts = event["hosts"][:]
        add(role, people=[name for name in event.get("people", []) if name],
            title=event.get("title", ""), time=event.get("time", ""))
        if role == "opening" or (role == "summary" and model["options"].get("repeat_chairs")):
            for name in (name for name in event.get("people", []) if name):
                add("chair", expert=name, time=event["time"])
        elif role == "talk":
            for name in (name for name in event.get("people", []) if name):
                add("speaker", expert=name, time=event["time"])
        elif role == "discussion":
            if model["options"].get("include_topics"):
                for s in slides:
                    if s["role"] == "topics" and not s.get("hidden"):
                        plan.append({"role": "topics", "template_slide": s["number"], "time": event["time"]})
            for name in (name for name in event.get("people", []) if name):
                add("guest", expert=name, time=event["time"])
    add("ending")
    return plan


def portrait_bytes(expert, ratio):
    if expert["photo"].startswith("file:"):
        # 旧版WPS头像已在分析阶段安全提取为本地PNG。
        data = Path(expert["photo"][5:]).read_bytes()
    else:
        package = read_package(expert["path"])
        data = package[expert["photo"]]
    selected = next((photo for photo in expert.get("photos", [])
                     if photo.get("image") == expert.get("photo")), {})
    with Image.open(io.BytesIO(data)) as photo:
        photo = ImageOps.exif_transpose(photo).convert("RGB")
        # 专家资料PPT可能依靠图片对象旋转或翻转显示正确方向，替换前需还原视觉效果。
        if selected.get("flip_h"):
            photo = ImageOps.mirror(photo)
        if selected.get("flip_v"):
            photo = ImageOps.flip(photo)
        rotation = float(selected.get("rotation", 0) or 0)
        if rotation:
            photo = photo.rotate(-rotation, expand=True)
        target_h = min(1800, max(600, photo.height))
        target_w = max(1, round(target_h * ratio))
        source_ratio = photo.width / max(photo.height, 1)
        # 窄幅全身照填入较宽照片框时从顶部开始裁切，优先保留完整头部和肩部。
        # 普通证件照继续轻微上移，避免顶部留白并保持原有构图。
        vertical_center = 0.0 if ratio > source_ratio * 1.35 else 0.4
        fitted = ImageOps.fit(photo, (target_w, target_h), method=Image.Resampling.LANCZOS,
                              centering=(0.5, vertical_center))
        output = io.BytesIO()
        fitted.save(output, "PNG", optimize=False)
        return output.getvalue()


def replace_photo(node, rels, package, expert, shape, cache):
    blip = node.find(".//a:blip", NS)
    if blip is None:
        raise ValueError("照片区域没有可替换图片")
    metadata = node.find('.//p:cNvPr', NS)
    if metadata is not None:
        metadata.set('descr', expert['name'] + '资料照片')
        metadata.attrib.pop('title', None)
    ratio = shape["bbox"][2] / max(1, shape["bbox"][3])
    selected = next((photo for photo in expert.get("photos", [])
                     if photo.get("image") == expert.get("photo")), {})
    key = (expert["path"], expert["photo"], round(ratio, 4), selected.get("rotation", 0),
           selected.get("flip_h", False), selected.get("flip_v", False))
    if key not in cache:
        target = f"ppt/media/autoppt_{len(cache) + 1}.png"
        package[target] = portrait_bytes(expert, ratio)
        cache[key] = target
    rid = "rIdAutoPortrait"
    occupied = {r.get("Id") for r in rels}
    while rid in occupied:
        rid += "x"
    ET.SubElement(rels, tag("rel", "Relationship"), Id=rid,
                  Type=NS["r"] + "/image", Target="../media/" + posixpath.basename(cache[key]))
    blip.set(tag("r", "embed"), rid)
    blip.attrib.pop(tag("r", "link"), None)
    fill = node.find("p:blipFill", NS)
    if fill is not None:
        for child in list(fill):
            if ET.QName(child).localname in ("srcRect", "tile", "stretch"):
                fill.remove(child)
        stretch = ET.SubElement(fill, tag("a", "stretch"))
        ET.SubElement(stretch, tag("a", "fillRect"))


def prune_package(package):
    """删除不再引用的旧页面、旧照片和旧备注，输出文件不携带旧专家资料。"""
    kept = {"[Content_Types].xml"}
    queue = [""]
    while queue:
        part = queue.pop()
        rp = "_rels/.rels" if not part else rel_path(part)
        if rp not in package or rp in kept:
            continue
        kept.add(rp)
        for relation in xml(package[rp]):
            if relation.get("TargetMode") == "External":
                continue
            target = resolve(part, relation.get("Target", ""))
            if target not in package:
                raise ValueError(f"生成文件存在无效引用：{target}")
            if target not in kept:
                kept.add(target)
                queue.append(target)
    ct = xml(package["[Content_Types].xml"])
    for item in list(ct):
        if item.tag == tag("ct", "Override") and item.get("PartName", "").lstrip("/") not in kept:
            ct.remove(item)
    package["[Content_Types].xml"] = encoded(ct)
    return {k: v for k, v in package.items() if k in kept}


def generate(model, destination):
    started = time.perf_counter()
    missing, validation_messages = validate_model(model)
    plan = page_plan(model)
    package = read_package(model["template"]["path"])
    profile = {s["number"]: s for s in model["template"]["slides"]}
    experts = {e["name"]: e for e in model["experts"] if e.get("name")}
    agenda = model["agenda"]
    messages, output_plan, cache, summaries = list(validation_messages), [], {}, {}
    if missing:
        messages.append(issue("draft_missing", "待核对版本，缺少简介：" + "、".join(missing)))
    if any(s["role"] == "topics" for s in profile.values()):
        messages.append(issue("topics", "讨论话题按原文保留，尚未校验其医学内容" if model["options"].get("include_topics") else "已按设置排除模板中的讨论话题页"))
    pres = xml(package["ppt/presentation.xml"])
    refs = pres.find("p:sldIdLst", NS)
    for n in list(refs):
        refs.remove(n)
    pres_rels = xml(package[rel_path("ppt/presentation.xml")])
    for n in list(pres_rels):
        if n.get("Type", "").endswith(("/slide", "/notesMaster", "/commentAuthors")):
            pres_rels.remove(n)
    # 自定义放映、章节及旧备注母版会引用原页面，生成时一并清除这些旧引用。
    for n in list(pres):
        if ET.QName(n).localname in ("custShowLst", "notesMasterIdLst", "extLst"):
            pres.remove(n)
    ct = xml(package["[Content_Types].xml"])
    if not any(n.get("Extension") == "png" for n in ct):
        ET.SubElement(ct, tag("ct", "Default"), Extension="png", ContentType="image/png")

    def expert_data(name):
        fallback = {"name": name, "hospital": "", "bio": ["简介待补充"], "photo": "", "photos": []}
        result = dict(experts.get(name, fallback))
        details = agenda.get("people", {}).get(name, {})
        result["display_hospital"] = details.get("hospital") or result.get("hospital", "")
        result["display_title"] = details.get("display_title", "")
        if result.get("photo") and result["photo"] not in [photo.get("image") for photo in result.get("photos", [])]:
            result["photo"] = ""
        return result

    # 同一模板页可能被多次复用，兼容提示按布局去重，避免核对报告重复刷屏。
    fallback_notices = set()
    missing_field_notices = set()
    for item in plan:
        source = profile[item["template_slide"]]
        original = xml(package[source["part"]])
        fields = item.get("fields", source["fields"])
        role = item["role"]
        bio_pages = [None]
        person = expert_data(item["expert"]) if item.get("expert") else None
        fallback_notice = (role, source["number"])
        if item.get("template_fallback") and fallback_notice not in fallback_notices:
            fallback_notices.add(fallback_notice)
            messages.append(issue("template_fallback", f"{ROLES[role]}使用模板第{source['number']}页的相近布局", page=source["number"]))
        missing_field_notice = (source["number"], tuple(item.get("missing_fields", ())))
        if item.get("missing_fields") and missing_field_notice not in missing_field_notices:
            missing_field_notices.add(missing_field_notice)
            messages.append(issue("template_fields_missing", f"模板第{source['number']}页缺少可填充区域：{'、'.join(item['missing_fields'])}，该部分保留空白或原布局",
                                  page=source["number"]))
        summary = None
        if person and fields.get("bio"):
            node = find_shape(original, fields["bio"])
            summary = select_single_page_bio(node, shape_info(source, fields['bio']), person)
            bio_pages = [summary['lines']]
            summaries.setdefault(person['name'], []).append(summary)
            if summary.get("fit_adjusted"):
                messages.append(issue("bio_fit", f"{person['name']}的简介已按模板空间自动缩放或精简", expert=person["name"]))
            if summary['missing'] and not any(m.get('expert') == person['name'] and m['code'] == 'bio_missing_fields' for m in messages):
                messages.append(issue('bio_missing_fields', person['name'] + '原文未明确：' + '、'.join(summary['missing']), expert=person['name']))
        for continuation, body_lines in enumerate(bio_pages):
            root = copy.deepcopy(original)
            root.attrib.pop("show", None)
            rp = rel_path(source["part"])
            rels = xml(package[rp]) if rp in package else ET.Element(tag("rel", "Relationships"), nsmap={None: NS["rel"]})
            for relation in list(rels):
                if relation.get("Type", "").endswith(("/notesSlide", "/comments", "/slide")):
                    rels.remove(relation)
            clear_links(root)
            # 所有页面的页眉会议名称都使用单行自适应，结束页也不能保留模板旧标题。
            for sid in fields.get("meeting", []):
                node = find_shape(root, sid)
                overflow, _ = fit_meeting_title(node, shape_info(source, sid), agenda.get("title", ""))
                if overflow:
                    messages.append(issue("text_fit", f"第{len(output_plan)+1}页会议标题可能溢出"))
            if role == "cover":
                title_id = fields.get("title")
                metadata_ids = fields.get("metadata", [])
                if title_id and title_id not in metadata_ids:
                    sid = title_id
                    fit_lines(find_shape(root, sid), shape_info(source, sid), [agenda.get("title", "")], minimum=24)
                for sid in metadata_ids:
                    node = find_shape(root, sid)
                    lines = paragraphs(node)
                    if sid == title_id:
                        # 标题与日期共用文本框时，按原段落数量分别替换，避免日期样式被标题覆盖。
                        title_slots = sum(not DATE.search(line) and "主办" not in line for line in lines)
                        title_lines = iter(balanced_lines(agenda.get("title", ""), max(1, title_slots)))
                        new_lines = []
                        for line in lines:
                            if DATE.search(line):
                                new_lines.append(agenda.get("date", ""))
                            elif "主办" in line:
                                new_lines.append("主办单位：" + agenda["organizer"] if agenda.get("organizer") else "")
                            else:
                                new_lines.append(next(title_lines, ""))
                        set_text(node, new_lines)
                        continue
                    new_lines = []
                    for line in lines:
                        if DATE.search(line):
                            line = DATE.sub(agenda.get("date", ""), line)
                        if "主办单位" in line:
                            line = "主办单位：" + agenda["organizer"] if agenda.get("organizer") else ""
                        if line:
                            new_lines.append(line)
                    set_text(node, new_lines)
            elif role == "ending":
                for sid in fields.get("ending_content", []):
                    node = find_shape(root, sid)
                    if node is None:
                        continue
                    new_lines = []
                    title_written = False
                    for line in paragraphs(node):
                        if DATE.search(line):
                            if agenda.get("date"):
                                new_lines.append(DATE.sub(agenda["date"], line))
                        elif agenda.get("title") and not title_written:
                            new_lines.append(agenda["title"])
                            title_written = True
                    set_text(node, new_lines)
            elif person:
                title = display_name(person["name"]) + ("  " + person["display_title"] if person["display_title"] else "")
                identity_lines = [title]
                if fields.get("hospital"):
                    fit_lines(find_shape(root, fields["hospital"]), shape_info(source, fields["hospital"]), [person["display_hospital"]], minimum=14)
                else:
                    identity_lines.append(person["display_hospital"])
                if fields.get("identity"):
                    identity_id = fields["identity"]
                    fit_lines(find_shape(root, identity_id), shape_info(source, identity_id), identity_lines, minimum=14)
                if fields.get("bio") and summary:
                    body = find_shape(root, fields["bio"])
                    set_text(body, body_lines)
                # 回退复用其他人物页时，统一替换页面中识别到的旧角色文字。
                role_markers = {compact(value) for values in ROLE_TEXTS.values() for value in values}
                role_ids = [fields.get("role")]
                role_ids.extend(shape["id"] for shape in source["shapes"]
                                if shape["kind"] == "sp" and shape["text"]
                                and len(compact(shape["text"])) <= 16
                                and any(marker in compact(shape["text"]) for marker in role_markers))
                role_ids = list(dict.fromkeys(shape_id for shape_id in role_ids if shape_id))
                label = ROLE_LABELS[role]
                for role_id in role_ids:
                    role_node = find_shape(root, role_id)
                    fit_lines(role_node, shape_info(source, role_id), [label], minimum=10)
                photo_node = find_shape(root, fields["photo"]) if fields.get("photo") else None
                if person.get("photo") and photo_node is not None:
                    photo_shape = shape_info(source, fields["photo"])
                    frame_shape = photo_frame_shape(source, photo_shape)
                    replacement_shape = photo_shape
                    if frame_shape:
                        # 各类人物页的原照片对象高度不同，统一对齐到后方可见圆角框。
                        align_shape_bbox(photo_node, photo_shape["bbox"], frame_shape["bbox"])
                        replacement_shape = {**photo_shape, "bbox": frame_shape["bbox"]}
                    replace_photo(photo_node, rels, package, person, replacement_shape, cache)
                    if role_ids:
                        # 图片充满框后，将角色标签置于照片上层，避免标签被照片遮挡。
                        for role_id in role_ids:
                            bring_to_front(find_shape(root, role_id))
                elif photo_node is not None:
                    photo_node.getparent().remove(photo_node)
                    messages.append(issue("photo_missing", f"{person['name']}未填入照片，旧照片已清除"))
            elif role in EVENT_ROLES:
                lines = []
                for name in item["people"]:
                    p = expert_data(name)
                    lines.append("  ".join(v for v in (display_name(name), p["display_title"], p["display_hospital"]) if v))
                for field, values, minimum in [("people", lines, 18), ("title", [item["title"]], 24)]:
                    shape_ids = fields.get(field)
                    if not shape_ids:
                        continue
                    shape_ids = shape_ids if isinstance(shape_ids, list) else [shape_ids]
                    for index, sid in enumerate(shape_ids):
                        if field == "people" and len(shape_ids) > 1:
                            current = values[index:index + 1] if index < len(shape_ids) - 1 else values[index:]
                        else:
                            current = values
                        overflow, _ = fit_lines(find_shape(root, sid), shape_info(source, sid), current, minimum)
                        if overflow:
                            messages.append(issue("text_fit", f"第{len(output_plan)+1}页{field}区域可能溢出，请检查预览"))
                # 多头像讨论页按名单顺序替换照片，多余旧照片直接清除。
                photo_ids = fields.get("people_photos", [])
                photo_ids = photo_ids if isinstance(photo_ids, list) else [photo_ids]
                for index, photo_id in enumerate(photo_ids):
                    photo_node = find_shape(root, photo_id)
                    if index >= len(item["people"]):
                        photo_node.getparent().remove(photo_node)
                        continue
                    person = expert_data(item["people"][index])
                    if person.get("photo"):
                        replace_photo(photo_node, rels, package, person, shape_info(source, photo_id), cache)
                    else:
                        photo_node.getparent().remove(photo_node)
            number = len(output_plan) + 1
            part = f"ppt/slides/autoppt{number}.xml"
            package[part] = encoded(root)
            package[rel_path(part)] = encoded(rels)
            rid = f"rIdAutoSlide{number}"
            ET.SubElement(refs, tag("p", "sldId"), id=str(255 + number), attrib={tag("r", "id"): rid})
            ET.SubElement(pres_rels, tag("rel", "Relationship"), Id=rid, Type=NS["r"] + "/slide", Target=f"slides/autoppt{number}.xml")
            ET.SubElement(ct, tag("ct", "Override"), PartName="/" + part,
                          ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml")
            output_plan.append({**item, "page": number, "continuation": continuation + 1,
                                "selected_bio": body_lines if person else None,
                                "source_expert": person.get("source", "缺少资料") if person else None})
    package["ppt/presentation.xml"] = encoded(pres)
    package[rel_path("ppt/presentation.xml")] = encoded(pres_rels)
    package["[Content_Types].xml"] = encoded(ct)
    # 删除封面缩略图，避免Office文件列表显示原会议封面。
    if "_rels/.rels" in package:
        root_rels = xml(package["_rels/.rels"])
        for r in list(root_rels):
            if r.get("Type", "").endswith("/thumbnail"):
                root_rels.remove(r)
        package["_rels/.rels"] = encoded(root_rels)
    if "docProps/app.xml" in package:
        app = xml(package["docProps/app.xml"])
        for n in list(app):
            local = ET.QName(n).localname
            if local == "Slides":
                n.text = str(len(output_plan))
            elif local == "Notes":
                n.text = "0"
            elif local in ("TitlesOfParts", "HeadingPairs"):
                app.remove(n)
        package["docProps/app.xml"] = encoded(app)
    package = prune_package(package)
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
            for name, data in package.items():
                archive.writestr(name, data)
        # 再读取实际输出，结构错误仍会中断，内容缺项记录到报告并继续交付。
        checked = read_deck(temporary)
        if len(checked["slides"]) != len(output_plan):
            raise ValueError("生成后的页面数量不一致")
        content_warnings = []
        for page, spec in zip(checked["slides"], output_plan):
            if spec.get("expert") and compact(spec["expert"]) not in compact(page["text"]):
                content_warnings.append(f"第{spec['page']}页缺少对应专家姓名")
            for name in spec.get('people', []):
                if compact(name) not in compact(page['text']):
                    content_warnings.append(f"第{spec['page']}页名单缺少{name}")
            if spec.get('role') == 'talk' and spec.get('title') and compact(spec['title']) not in compact(page['text']):
                content_warnings.append(f"第{spec['page']}页讲题写入不完整")
            # 校验本次精选内容，完整原文保留在核对报告中供追溯。
            for line in spec.get('selected_bio') or []:
                if compact(line) not in compact(page['text']):
                    content_warnings.append(f"{spec['expert']}的精选简介未完整写入")
        current_names = {name for event in agenda.get('events', [])
                         for name in event.get('people', []) + event.get('hosts', []) if name}
        old_names = set()
        for source in profile.values():
            identity_ids = set()
            for field in ('identity', 'people'):
                values = source['fields'].get(field, [])
                identity_ids.update(values if isinstance(values, list) else [values])
            identity_ids.discard(None)
            identity_ids.discard('')
            identity_text = '\n'.join(s['text'] for s in source['shapes'] if s['id'] in identity_ids)
            old_names.update(names_in(identity_text))
        old_names -= current_names
        for page, spec in zip(checked['slides'], output_plan):
            if spec['role'] == 'topics':
                continue
            for name in old_names:
                if name in compact(page['text']):
                    content_warnings.append(f"第{spec['page']}页仍有模板旧人物{name}，请检查区域映射")
        for message in dict.fromkeys(content_warnings):
            messages.append(issue("output_content", message))
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()
    report = {"file": target.name, "pages": output_plan, "issues": messages, "input_issues": model["issues"],
              "bio_summaries": summaries,
              "draft": bool(missing or model["options"].get("draft") or validation_messages),
              "validation": {"structure": "passed", "content": "warnings" if any(m["code"] == "output_content" for m in messages) else "passed",
                             "visual": "not_rendered"},
              "metrics": {**model["metrics"], "generation_seconds": round(time.perf_counter() - started, 3), "ai_calls": 0}}
    target.with_suffix(".report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
