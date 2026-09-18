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

from .analyze import DATE, ROLES, issue, names_in
from .bio import summarize_bio
from .ooxml import NS, compact, encoded, paragraphs, read_deck, read_package, rel_path, resolve, tag, xml

ROLE_LABELS = {"chair": "大会主席", "host": "大会主持", "speaker": "大会讲者", "guest": "讨论嘉宾"}


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
        if width <= 0 or height <= 0 or not .65 <= height / ph <= 1.35:
            continue
        dx = abs(x - px) / pw
        dw = abs(width - pw) / pw
        db = abs((y + height) - (py + ph)) / ph
        if dx <= .08 and dw <= .08 and db <= .08:
            candidates.append((dx + dw + db, shape))
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


def set_text(node, lines, *, font=None, preserve_sizes=True, line_spacing=None):
    metadata = node.find('.//p:cNvPr', NS)
    if metadata is not None:
        metadata.attrib.pop('descr', None)
        metadata.attrib.pop('title', None)
    body = node.find("p:txBody", NS)
    if body is None:
        raise ValueError("选择的文本区域不支持直接编辑，请选择普通文本框")
    source = body.findall("a:p", NS)
    templates = [copy.deepcopy(p) for p in source if p.xpath('.//a:t/text()', namespaces=NS)] or [ET.Element(tag("a", "p"))]
    for para in source:
        body.remove(para)
    for index, value in enumerate(lines or [""]):
        original = templates[min(index, len(templates) - 1)]
        p = ET.SubElement(body, tag("a", "p"))
        ppr = original.find("a:pPr", NS)
        if ppr is not None:
            p.append(copy.deepcopy(ppr))
        if line_spacing is not None:
            # 专家简介固定字号后，通过统一行距保证八行仍落在模板文本框内。
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
        runs = [r for r in original.findall('a:r', NS) if ''.join(r.xpath('./a:t/text()', namespaces=NS)).strip()]
        # 空白占位符常带有异常字号，只继承实际文字的格式。
        props = [r.find('a:rPr', NS) for r in runs if r.find('a:rPr', NS) is not None]
        selected = props[0] if props else None
        rpr = copy.deepcopy(selected) if selected is not None else ET.Element(tag("a", "rPr"))
        clear_links(rpr)
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
    width = shape["bbox"][2] / 12700
    height = shape["bbox"][3] / 12700
    if body is not None:
        width -= (int(body.get("lIns", "91440")) + int(body.get("rIns", "91440"))) / 12700
        height -= (int(body.get("tIns", "45720")) + int(body.get("bIns", "45720"))) / 12700
    size = font_size or shape["font"]
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
    minimum = min(minimum, size)
    while size > minimum and (estimated_height(lines, width, size, family, node) > height
                              or any(text_width(line, measure_font(size, family)) > width * 4 for line in lines)):
        size = max(minimum, size - 1)
    overflow = estimated_height(lines, width, size, family, node) > height
    set_text(node, lines, font=size)
    return overflow, size


def select_single_page_bio(node, shape, person, font_size=16):
    """使用固定字号放置八行简介，不生成续页或截断任职名称。"""
    width, height, _, family = text_box(node, shape)
    summary = summarize_bio(person)
    lines = summary['lines'][:]
    size = int(font_size)
    line_spacing = 1.8
    # 专家简介统一使用16磅和180%行距，容量不足时提示调整内容或模板。
    if (estimated_height(lines, width, size, family, node, line_spacing) > height
            or any(text_width(x, measure_font(size, family)) > width for x in lines)):
        raise ValueError(f"{person['name']}的八行简介无法以{size}磅逐行放入模板，请精简原文或调整模板简介区域")
    selected_social = [x for x in lines if x in summary['social']]
    summary.update(lines=lines, font=size, line_spacing=line_spacing, selected_social=selected_social,
                   omitted_social=[x for x in summary['social'] if x not in selected_social])
    return summary


def validate_model(model):
    errors = []
    agenda, profile = model["agenda"], model["template"]
    if not agenda.get("title") or not agenda.get("date"):
        errors.append("会议名称或日期为空")
    if not agenda.get("events"):
        errors.append("日程没有可生成的环节")
    experts = {e["name"]: e for e in model["experts"]}
    if len(experts) != len(model["experts"]) or "" in experts:
        errors.append("专家姓名为空或存在同名资料")
    required = set()
    previous_end = None
    for event in agenda["events"]:
        required.update(event.get("people", []))
        required.update(event.get("hosts", []))
        if event.get("kind") not in ("opening", "talk", "discussion", "summary"):
            errors.append("日程包含不支持的环节类型")
        if not event.get("people") or not event.get("title"):
            errors.append(f"{event.get('time', '')}缺少人员或内容")
        times = re.findall(r"(\d{1,2}):([0-5]\d)", event.get("time", ""))
        if len(times) != 2 or any(int(h) > 23 for h, _ in times):
            errors.append(f"时间格式无效：{event.get('time', '')}")
        else:
            start, end = [int(h) * 60 + int(m) for h, m in times]
            if end <= start or (previous_end is not None and start < previous_end):
                errors.append(f"日程时间重叠或顺序有误：{event['time']}")
            previous_end = end
    missing = sorted(required - set(experts))
    if missing and not model["options"].get("draft"):
        errors.append("缺少专家简介：" + "、".join(missing) + "。可补充资料或选择生成待核对版本")
    for name in required & set(experts):
        expert = experts[name]
        if not expert.get('photo_confirmed', True) and not model['options'].get('draft'):
            errors.append(f'{name}有多张候选照片，请在专家资料中确认')
        if not expert.get("bio") and not model["options"].get("draft"):
            errors.append(f"{name}简介为空")
        if expert.get("photo") and expert["photo"] not in [p["image"] for p in expert["photos"]]:
            errors.append(f"{name}选择的照片不存在")
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
                    errors.append(f"模板第{slide['number']}页区域不存在")
                elif field == "photo" and shapes[sid]["kind"] != "pic":
                    errors.append(f"模板第{slide['number']}页照片区域类型无效")
                elif field != "photo" and shapes[sid]["kind"] != "sp":
                    errors.append(f"模板第{slide['number']}页需要选择普通文本框")
                if field not in ("meeting", "metadata"):
                    assigned.append(sid)
        if len(set(assigned)) != len(assigned):
            errors.append(f"模板第{slide['number']}页多个字段使用了同一区域")
    if errors:
        raise ValueError("；".join(errors))
    return missing


def page_plan(model):
    slides = model["template"]["slides"]
    plan = []

    def add(role, **data):
        candidates = [s for s in slides if s["role"] == role and not s.get("hidden")]
        if not candidates and role in ROLE_LABELS:
            candidates = [s for s in slides if s["role"] in ROLE_LABELS and not s.get("hidden")]
        if not candidates:
            raise ValueError(f"模板缺少{ROLES[role]}页面，请在模板设置中指定对应页面")
        chosen = candidates[0]
        required = ["bio", "identity", "photo"] if role in ROLE_LABELS else (["people", "title"] if role in ("opening", "talk", "discussion", "summary") else (["title"] if role == "cover" else []))
        for field in required:
            if not chosen["fields"].get(field):
                raise ValueError(f"模板第{chosen['number']}页缺少{field}区域映射")
        plan.append({"role": role, "template_slide": chosen["number"], **data})

    add("cover")
    last_hosts = None
    for event in model["agenda"]["events"]:
        role = event["kind"]
        if role in ("talk", "discussion") and event.get("hosts") and event["hosts"] != last_hosts:
            for name in event["hosts"]:
                add("host", expert=name, time=event["time"])
            last_hosts = event["hosts"][:]
        add(role, people=event["people"], title=event["title"], time=event["time"])
        if role == "opening" or (role == "summary" and model["options"].get("repeat_chairs")):
            for name in event["people"]:
                add("chair", expert=name, time=event["time"])
        elif role == "talk":
            for name in event["people"]:
                add("speaker", expert=name, time=event["time"])
        elif role == "discussion":
            if model["options"].get("include_topics"):
                for s in slides:
                    if s["role"] == "topics" and not s.get("hidden"):
                        plan.append({"role": "topics", "template_slide": s["number"], "time": event["time"]})
            for name in event["people"]:
                add("guest", expert=name, time=event["time"])
    add("ending")
    return plan


def portrait_bytes(expert, ratio):
    package = read_package(expert["path"])
    data = package[expert["photo"]]
    with Image.open(io.BytesIO(data)) as photo:
        photo = ImageOps.exif_transpose(photo).convert("RGB")
        target_h = min(1800, max(600, photo.height))
        target_w = max(1, round(target_h * ratio))
        # 等比放大并裁切到图片框尺寸，人物略微上移，避免顶部留白并尽量保留头部。
        fitted = ImageOps.fit(photo, (target_w, target_h), method=Image.Resampling.LANCZOS,
                              centering=(0.5, 0.4))
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
    key = (expert["path"], expert["photo"], round(ratio, 4))
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
    missing = validate_model(model)
    plan = page_plan(model)
    package = read_package(model["template"]["path"])
    profile = {s["number"]: s for s in model["template"]["slides"]}
    experts = {e["name"]: e for e in model["experts"]}
    agenda = model["agenda"]
    messages, output_plan, cache, summaries = [], [], {}, {}
    if missing:
        messages.append(issue("draft_missing", "待核对版本，缺少简介：" + "、".join(missing), "error"))
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
        fallback = {"name": name, "hospital": "", "bio": ["简介待补充"], "photo": ""}
        result = dict(experts.get(name, fallback))
        details = agenda["people"].get(name, {})
        result["display_hospital"] = details.get("hospital") or result["hospital"]
        result["display_title"] = details.get("display_title", "")
        return result

    for item in plan:
        source = profile[item["template_slide"]]
        original = xml(package[source["part"]])
        fields = source["fields"]
        role = item["role"]
        bio_pages = [None]
        person = expert_data(item["expert"]) if item.get("expert") else None
        if person:
            node = find_shape(original, fields["bio"])
            summary = select_single_page_bio(
                node, shape_info(source, fields['bio']), person,
                font_size=model["options"].get("bio_font_size", 16))
            bio_pages = [summary['lines']]
            summaries.setdefault(person['name'], []).append(summary)
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
            for sid in fields.get("meeting", []):
                node = find_shape(root, sid)
                overflow, _ = fit_lines(node, shape_info(source, sid), [agenda["title"]], minimum=16)
                if overflow:
                    messages.append(issue("text_fit", f"第{len(output_plan)+1}页会议标题可能溢出"))
            if role == "cover":
                sid = fields["title"]
                fit_lines(find_shape(root, sid), shape_info(source, sid), [agenda["title"]], minimum=24)
                for sid in fields.get("metadata", []):
                    node = find_shape(root, sid)
                    lines = paragraphs(node)
                    new_lines = []
                    for line in lines:
                        if DATE.search(line):
                            line = DATE.sub(agenda["date"], line)
                        if "主办单位" in line:
                            line = "主办单位：" + agenda["organizer"] if agenda.get("organizer") else ""
                        if line:
                            new_lines.append(line)
                    set_text(node, new_lines)
            elif person:
                title = person["name"] + ("  " + person["display_title"] if person["display_title"] else "")
                identity = find_shape(root, fields["identity"])
                identity_lines = [title]
                if fields.get("hospital"):
                    fit_lines(find_shape(root, fields["hospital"]), shape_info(source, fields["hospital"]), [person["display_hospital"]], minimum=14)
                else:
                    identity_lines.append(person["display_hospital"])
                set_text(identity, identity_lines)
                body = find_shape(root, fields["bio"])
                set_text(body, body_lines, font=summary['font'], line_spacing=summary['line_spacing'])
                if fields.get("role"):
                    role_node = find_shape(root, fields["role"])
                    label = ROLE_LABELS[role]
                    fit_lines(role_node, shape_info(source, fields["role"]), [label], minimum=10)
                photo_node = find_shape(root, fields["photo"])
                if person.get("photo"):
                    photo_shape = shape_info(source, fields["photo"])
                    frame_shape = photo_frame_shape(source, photo_shape)
                    replacement_shape = photo_shape
                    if frame_shape:
                        # 各类人物页的原照片对象高度不同，统一对齐到后方可见圆角框。
                        align_shape_bbox(photo_node, photo_shape["bbox"], frame_shape["bbox"])
                        replacement_shape = {**photo_shape, "bbox": frame_shape["bbox"]}
                    replace_photo(photo_node, rels, package, person, replacement_shape, cache)
                    if fields.get("role"):
                        # 图片充满框后，将角色标签置于照片上层，避免标签被照片遮挡。
                        bring_to_front(find_shape(root, fields["role"]))
                else:
                    photo_node.getparent().remove(photo_node)
                    messages.append(issue("photo_missing", f"{person['name']}未填入照片，旧照片已清除"))
            elif role in ("opening", "summary", "talk", "discussion"):
                lines = []
                for name in item["people"]:
                    p = expert_data(name)
                    lines.append("  ".join(v for v in (name, p["display_title"], p["display_hospital"]) if v))
                for field, values, minimum in [("people", lines, 18), ("title", [item["title"]], 24)]:
                    sid = fields[field]
                    overflow, _ = fit_lines(find_shape(root, sid), shape_info(source, sid), values, minimum)
                    if overflow:
                        messages.append(issue("text_fit", f"第{len(output_plan)+1}页{field}区域可能溢出，请检查预览"))
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
        # 再读取实际输出，确认页面顺序和专家文字确实写入文件。
        checked = read_deck(temporary)
        if len(checked["slides"]) != len(output_plan):
            raise ValueError("生成后的页面数量不一致")
        for page, spec in zip(checked["slides"], output_plan):
            if spec.get("expert") and compact(spec["expert"]) not in compact(page["text"]):
                raise ValueError(f"第{spec['page']}页缺少对应专家姓名")
            for name in spec.get('people', []):
                if compact(name) not in compact(page['text']):
                    raise ValueError(f"第{spec['page']}页名单缺少{name}")
            if spec.get('role') == 'talk' and compact(spec['title']) not in compact(page['text']):
                raise ValueError(f"第{spec['page']}页讲题写入不完整")
            # 校验本次精选内容，完整原文保留在核对报告中供追溯。
            for line in spec.get('selected_bio') or []:
                if compact(line) not in compact(page['text']):
                    raise ValueError(f"{spec['expert']}的精选简介未完整写入")
        current_names = {name for event in agenda['events'] for name in event['people'] + event['hosts']}
        old_names = set()
        for source in profile.values():
            identity_ids = {source['fields'].get('identity'), source['fields'].get('people')}
            identity_text = '\n'.join(s['text'] for s in source['shapes'] if s['id'] in identity_ids)
            old_names.update(names_in(identity_text, current_names))
        old_names -= current_names
        for page, spec in zip(checked['slides'], output_plan):
            if spec['role'] == 'topics':
                continue
            for name in old_names:
                if name in compact(page['text']):
                    raise ValueError(f"第{spec['page']}页仍有模板旧人物{name}，请检查区域映射")
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()
    report = {"file": target.name, "pages": output_plan, "issues": messages, "input_issues": model["issues"],
              "bio_summaries": summaries,
              "draft": bool(missing or model["options"].get("draft")), "validation": {"structure": "passed", "content": "passed", "visual": "not_rendered"},
              "metrics": {**model["metrics"], "generation_seconds": round(time.perf_counter() - started, 3), "ai_calls": 0}}
    target.with_suffix(".report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
