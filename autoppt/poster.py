"""从日程图片和专家头像生成会议海报，所有识别与绘制均在本机完成。"""

from __future__ import annotations

import base64
import io
import re
import statistics
import threading
import time
import zipfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from .analyze import DATE, TIME, classify_event, hospital_in, names_in, normalized_text
from .ooxml import MAX_EXPANDED, image_thumbnail

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
OCR_LOCK = threading.Lock()
OCR_ENGINE = None
LOGO_UNITS = {"SSPCA": "山东省亚健康防治协会"}


def clean_name(value):
    """从头像文件名中提取姓名，保留中文姓名和间隔点。"""
    value = Path(value).stem.strip()
    value = re.sub(r"^[\d\s._-]+", "", value)
    value = re.sub(r"(?:教授|主任|医生|头像|照片|专家)+$", "", value).strip()
    hit = re.search(r"[\u4e00-\u9fff·]{2,6}", value)
    return hit.group(0) if hit else value[:20]


def decoded_zip_name(entry):
    name = entry.filename
    if not entry.flag_bits & 0x800:
        try:
            name = name.encode("cp437").decode("gbk")
        except UnicodeError:
            pass
    return name.replace("\\", "/")


def unpack_portraits(sources, destination):
    """安全解压头像压缩包，并将单独上传的图片统一复制到任务目录。"""
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    result = []
    for source_index, source in enumerate(map(Path, sources)):
        if source.suffix.lower() in IMAGE_EXTENSIONS:
            target = destination / f"{source_index:03d}_{source.name}"
            target.write_bytes(source.read_bytes())
            result.append(target)
            continue
        if source.suffix.lower() != ".zip":
            raise ValueError(f"不支持的头像文件格式：{source.name}")
        with zipfile.ZipFile(source) as archive:
            entries = archive.infolist()
            if len(entries) > 1000 or sum(e.file_size for e in entries) > MAX_EXPANDED:
                raise ValueError("头像压缩包解压规模超过限制")
            for entry_index, entry in enumerate(entries):
                if entry.is_dir():
                    continue
                normalized = decoded_zip_name(entry)
                if normalized.startswith("/") or ".." in normalized.split("/") or ":" in normalized:
                    raise ValueError("头像压缩包包含不安全路径")
                leaf = Path(normalized).name
                if Path(leaf).suffix.lower() not in IMAGE_EXTENSIONS:
                    continue
                target = destination / f"{source_index:03d}_{entry_index:03d}_{leaf}"
                target.write_bytes(archive.read(entry))
                result.append(target)
    if not result:
        raise ValueError("未找到PNG、JPG或WEBP格式的专家头像")
    return result


def get_ocr_engine():
    global OCR_ENGINE
    if OCR_ENGINE is None:
        try:
            from rapidocr import RapidOCR
        except ImportError as exc:
            raise ValueError("缺少本地OCR组件，请重新运行安装程序") from exc
        OCR_ENGINE = RapidOCR()
    return OCR_ENGINE


def read_agenda_image(path):
    """返回带坐标的OCR文字，坐标用于恢复日程表的行列关系。"""
    with Image.open(path) as source:
        width, height = source.size
    with OCR_LOCK:
        result = get_ocr_engine()(str(path))
    if result is None or result.boxes is None:
        raise ValueError("日程图片未识别出文字，请上传更清晰的原图")
    items = []
    for box, text, score in zip(result.boxes, result.txts, result.scores):
        xs = [float(point[0]) for point in box]
        ys = [float(point[1]) for point in box]
        items.append({"text": normalized_text(text), "score": round(float(score), 4),
                      "bbox": [min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)]})
    return items, width, height


def row_groups(items):
    """按文字中心线组合OCR碎片，处理姓名被拆成多个识别框的情况。"""
    if not items:
        return []
    heights = [item["bbox"][3] for item in items if item["bbox"][3] > 0]
    tolerance = max(12, statistics.median(heights) * .55)
    rows = []
    for item in sorted(items, key=lambda value: (value["bbox"][1] + value["bbox"][3] / 2, value["bbox"][0])):
        center = item["bbox"][1] + item["bbox"][3] / 2
        row = next((candidate for candidate in reversed(rows) if abs(candidate["center"] - center) <= tolerance), None)
        if row is None:
            row = {"center": center, "items": []}
            rows.append(row)
        row["items"].append(item)
        row["center"] = sum(v["bbox"][1] + v["bbox"][3] / 2 for v in row["items"]) / len(row["items"])
    for row in rows:
        row["items"].sort(key=lambda value: value["bbox"][0])
        row["text"] = "".join(value["text"] for value in row["items"])
    return rows


def person_detail(text, name):
    compact = re.sub(r"\s+", "", text)
    position = compact.find(name)
    if position < 0:
        return {"hospital": "", "title": "教授"}
    tail = compact[position + len(name):]
    title_hit = re.match(r"(教授|主任医师|副主任医师|主治医师|医生)", tail)
    title = title_hit.group(1) if title_hit else "教授"
    if title_hit:
        tail = tail[title_hit.end():]
    return {"hospital": hospital_in(tail), "title": title}


def names_from_rows(rows, known):
    return list(dict.fromkeys(name for row in rows for name in names_in(row["text"], known)))


def organizer_from_logo(items, width, height):
    """优先从日程左上角标志区域识别主办单位，并兼容常见标志简称。"""
    logo_text = "".join(item["text"] for item in items
                        if item["bbox"][0] < width * .28 and item["bbox"][1] < height * .2)
    for abbreviation, unit in LOGO_UNITS.items():
        if abbreviation.lower() in logo_text.lower():
            return unit
    hit = re.search(r"([\u4e00-\u9fff]{4,24}(?:协会|学会|基金会|医院|公司))", logo_text)
    return hit[1] if hit else ""


def parse_agenda_image(path, portrait_names):
    """依据文字坐标解析会议元数据、主持信息和日程行。"""
    items, width, height = read_agenda_image(path)
    rows = row_groups(items)
    full_text = "\n".join(row["text"] for row in rows)
    known = [name for name in portrait_names if name]
    title_candidates = []
    excluded = ("会议日程", "会议时间", "腾讯会议", "会议主席", "主办单位")
    for item in items:
        x, y, w, h = item["bbox"]
        text = item["text"]
        if y < height * .28 and "会议" in text and not any(word in text for word in excluded):
            title_candidates.append((h * max(1, len(text)) ** .2, text))
    title = max(title_candidates, default=(0, "会议海报"))[1]
    date_hit = DATE.search(full_text)
    date = f"{date_hit[1]}年{int(date_hit[2])}月{int(date_hit[3])}日" if date_hit else ""
    meeting_time = ""
    for row in rows:
        if "会议时间" in row["text"]:
            hit = TIME.search(row["text"])
            meeting_time = hit[0] if hit else ""
            break
    meeting_code = ""
    code_hit = re.search(r"(?:腾讯会议|会议号|会议ID)\s*[:：]\s*([\d-]{6,})", full_text, re.I)
    if code_hit:
        meeting_code = code_hit[1]
    organizer = organizer_from_logo(items, width, height)
    organizer_hit = re.search(r"主办单位\s*[:：]\s*([^\n]+)", full_text)
    if organizer_hit:
        organizer = organizer_hit[1].strip()
    chairs = []
    for row in rows:
        if "会议主席" in row["text"]:
            chairs = names_in(row["text"], known)
            break

    people = {name: {"hospital": "", "display_title": "教授"} for name in known}
    for row in rows:
        for name in names_in(row["text"], known):
            detail = person_detail(row["text"], name)
            if detail["hospital"]:
                people[name]["hospital"] = detail["hospital"]
            people[name]["display_title"] = detail["title"]

    time_rows = []
    for row in rows:
        hit = TIME.search(row["text"])
        # 会议总时段属于元数据，不能作为一条日程事件。
        if hit and "会议时间" not in row["text"] and min(item["bbox"][0] for item in row["items"]) < width * .32:
            time_rows.append((row["center"], hit[0], row))
    time_rows.sort()
    events = []
    for index, (center, value, time_row) in enumerate(time_rows):
        lower = (center + time_rows[index + 1][0]) / 2 if index + 1 < len(time_rows) else height
        upper = ((time_rows[index - 1][0] + center) / 2) if index else center - height * .03
        region = [row for row in rows if upper <= row["center"] < lower]
        # 分段主持条位于上一事件与下一事件之间，解析当前讲者时需要排除。
        content_region = [row for row in region if "主持" not in row["text"] and not row["text"].startswith("Section")]
        region_items = [item for row in content_region for item in row["items"]]
        person_names = names_from_rows(content_region, known)
        content_items = []
        for item in region_items:
            left, item_y, item_width, item_height = item["bbox"]
            text = item["text"]
            name_fragment = any((name.startswith(text) and len(text) <= len(name))
                                or ("教授" in text and text.startswith(name[-1])) for name in known)
            if (width * .18 <= left < width * .68 and not TIME.search(text)
                    and not names_in(text, known) and not name_fragment and "主持" not in text
                    and text not in ("时间", "内容", "讲者") and not text.startswith("Section")):
                content_items.append(item)
        content = min(content_items, key=lambda item: abs(item["bbox"][1] + item["bbox"][3] / 2 - center))["text"] if content_items else ""
        if not content and person_names:
            content = "互动讨论" if len(person_names) >= 3 else "专题分享"
        host_rows = [row for row in rows if row["center"] < center and "主持" in row["text"]]
        hosts = names_in(max(host_rows, key=lambda row: row["center"])["text"], known) if host_rows else []
        kind = classify_event(content)
        events.append({"time": value, "kind": kind, "title": content, "people": person_names,
                       "hosts": hosts if kind in ("talk", "discussion") else []})

    issues = []
    if not title or title == "会议海报":
        issues.append({"level": "error", "message": "会议名称未识别，请在生成前补充"})
    if not events:
        issues.append({"level": "error", "message": "日程时间段未识别，请上传更清晰的日程图片"})
    for event in events:
        if not event["title"]:
            issues.append({"level": "warning", "message": f"{event['time']}的环节内容需要核对"})
    return {"title": title, "date": date, "meeting_time": meeting_time, "meeting_code": meeting_code,
            "venue": "", "organizer": organizer, "chairs": chairs, "events": events, "people": people,
            "source": Path(path).name, "path": str(Path(path).resolve()),
            "ocr_text": full_text, "issues": issues,
            "ocr_items": len(items), "image_size": [width, height]}


def analyze_poster(template, agenda, portrait_sources, workdir):
    started = time.perf_counter()
    portraits = unpack_portraits(portrait_sources, Path(workdir) / "portraits")
    experts = []
    seen = set()
    for path in portraits:
        name = clean_name(path.name.split("_", 2)[-1])
        if not name or name in seen:
            continue
        seen.add(name)
        experts.append({"name": name, "hospital": "", "display_title": "教授", "role": "guest",
                        "path": str(path.resolve()), "source": path.name, "thumbnail": image_thumbnail(path.read_bytes())})
    schedule = parse_agenda_image(agenda, [expert["name"] for expert in experts])
    chair_set = set(schedule["chairs"])
    speaker_set = {name for event in schedule["events"] if event["kind"] == "talk" for name in event["people"]}
    guest_set = {name for event in schedule["events"] if event["kind"] == "discussion" for name in event["people"]}
    host_set = {name for event in schedule["events"] for name in event["hosts"]}
    issues = list(schedule.pop("issues"))
    for expert in experts:
        if expert["name"] in chair_set:
            expert["role"] = "chair"
        elif expert["name"] in host_set:
            expert["role"] = "host"
        elif expert["name"] in speaker_set:
            expert["role"] = "speaker"
        elif expert["name"] in guest_set:
            expert["role"] = "guest"
        detail = schedule["people"].get(expert["name"], {})
        expert["hospital"] = detail.get("hospital", "")
        expert["display_title"] = detail.get("display_title", "教授")
        if not expert["hospital"]:
            issues.append({"level": "warning", "message": f"{expert['name']}的单位未识别，可在生成前补充"})
    scheduled = set(schedule["chairs"])
    scheduled.update(name for event in schedule["events"] for name in event["people"] + event["hosts"])
    missing = sorted(scheduled - {expert["name"] for expert in experts})
    if missing:
        issues.append({"level": "error", "message": "日程中的人员缺少头像：" + "、".join(missing)})
    with Image.open(template) as source:
        template_width, template_height = source.size
    template_info = {"path": str(Path(template).resolve()), "name": Path(template).name,
                     "width": template_width, "height": template_height,
                     "thumbnail": image_thumbnail(Path(template).read_bytes(), (260, 520))}
    return {"version": 1, "kind": "poster", "agenda": schedule, "experts": experts,
            "template": template_info, "issues": issues,
            "metrics": {"analysis_seconds": round(time.perf_counter() - started, 3), "ai_calls": 0}}


def font_path(bold=False):
    candidates = [Path(r"C:\Windows\Fonts") / name for name in
                  (("msyhbd.ttc", "simhei.ttf", "msyh.ttc") if bold else ("msyh.ttc", "simhei.ttf"))]
    return next((str(path) for path in candidates if path.exists()), None)


def font(size, bold=False):
    path = font_path(bold)
    return ImageFont.truetype(path, max(12, int(size))) if path else ImageFont.load_default()


def accent_from_template(image):
    sample = image.copy()
    sample.thumbnail((160, 360))
    saturated = []
    for red, green, blue in sample.convert("RGB").getdata():
        maximum, minimum = max(red, green, blue), min(red, green, blue)
        if maximum - minimum > 45 and 55 < maximum < 245:
            saturated.append((red, green, blue))
    if not saturated:
        return (32, 139, 205)
    return tuple(int(statistics.median(pixel[channel] for pixel in saturated)) for channel in range(3))


def blend(color, other, ratio):
    return tuple(int(color[i] * (1 - ratio) + other[i] * ratio) for i in range(3))


def wrap_text(draw, text, text_font, max_width):
    lines = []
    for paragraph in str(text).splitlines() or [""]:
        current = ""
        for char in paragraph:
            candidate = current + char
            if current and draw.textbbox((0, 0), candidate, font=text_font)[2] > max_width:
                lines.append(current)
                current = char
            else:
                current = candidate
        lines.append(current)
    return lines


def centered_text(draw, box, text, text_font, fill, spacing=8):
    left, top, right, bottom = box
    lines = wrap_text(draw, text, text_font, right - left)
    line_height = text_font.size * 1.28
    y = top + max(0, (bottom - top - line_height * len(lines)) / 2)
    for line in lines:
        width = draw.textbbox((0, 0), line, font=text_font)[2]
        draw.text(((left + right - width) / 2, y), line, font=text_font, fill=fill)
        y += line_height + spacing


def compact_centered_text(draw, center_x, top, text, text_font, fill, max_width, line_gap=2):
    """从指定顶部连续绘制居中文字，返回下一行起点，用于压缩专家信息行距。"""
    y = top
    for line in wrap_text(draw, text, text_font, max_width):
        width = draw.textbbox((0, 0), line, font=text_font)[2]
        draw.text((center_x - width / 2, y), line, font=text_font, fill=fill)
        y += text_font.size * 1.14 + line_gap
    return y


def agenda_logo(path, size):
    """从日程左上角提取圆形机构标志，去除周围版面文字。"""
    if not path or not Path(path).exists():
        return None
    with Image.open(path) as source:
        source = ImageOps.exif_transpose(source).convert("RGBA")
        width, height = source.size
        crop = source.crop((int(width * .03), int(height * .015),
                            int(width * .18), int(height * .106)))
        crop = ImageOps.fit(crop, (size, size), Image.Resampling.LANCZOS, centering=(.5, .5))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
    crop.putalpha(mask)
    return crop


def portrait_circle(path, size, background):
    with Image.open(path) as source:
        source = ImageOps.exif_transpose(source).convert("RGBA")
        # 头像在圆形区域内水平和垂直居中，避免不同原图产生明显偏移。
        fitted = ImageOps.fit(source, (size, size), Image.Resampling.LANCZOS, centering=(.5, .5))
    base = Image.new("RGBA", (size, size), background + (255,))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
    base.paste(fitted, (0, 0), Image.composite(fitted.getchannel("A"), Image.new("L", fitted.size, 255), fitted.getchannel("A")))
    base.putalpha(mask)
    return base


def draw_section_title(draw, y, width, margin, title, accent):
    title_font = font(width * .020, True)
    text_width = draw.textbbox((0, 0), title, font=title_font)[2]
    center = width / 2
    dot = max(8, int(width * .005))
    gap = int(width * .012)
    for direction in (-1, 1):
        start = center + direction * (text_width / 2 + gap)
        for index in range(4):
            x = start + direction * index * dot * 2.2
            draw.ellipse((x - dot / 2, y + 25 - dot / 2, x + dot / 2, y + 25 + dot / 2), fill=accent)
    draw.text((center - text_width / 2, y), title, font=title_font, fill=accent)
    return y + int(width * .045)


def render_poster(model, output):
    """按照上传模板的颜色和比例绘制头像分组与会议日程。"""
    started = time.perf_counter()
    with Image.open(model["template"]["path"]) as source:
        template = ImageOps.exif_transpose(source).convert("RGB")
    width = max(1400, template.width)
    scale = width / 2480
    margin = int(width * .065)
    accent = accent_from_template(template)
    dark = blend(accent, (30, 20, 60), .38)
    light = blend(accent, (255, 255, 255), .88)
    groups = [("chair", "会议主席"), ("host", "会议主持"),
              ("speaker", "会议讲者"), ("guest", "讨论嘉宾")]
    grouped = [(role, label, [expert for expert in model["experts"] if expert["role"] == role])
               for role, label in groups]
    grouped = [group for group in grouped if group[2]]
    portrait_size = int(400 * scale)
    # 放大专家姓名和单位后同步增加头像行高，避免相邻分组拥挤。
    portrait_row = int(760 * scale)
    section_header = int(130 * scale)
    role_height = sum(section_header + ((len(experts) + 2) // 3) * portrait_row for _, _, experts in grouped)
    agenda_rows = []
    measure_draw = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    table_width = width - margin * 2
    title_width = table_width * .49 - int(28 * scale)
    people_width = table_width * .33 - int(24 * scale)
    agenda_title_font = font(36 * scale, True)
    agenda_people_font = font(34 * scale, True)
    last_hosts = None
    section_number = 0
    for event in model["agenda"]["events"]:
        hosts = tuple(event.get("hosts", []))
        if hosts and hosts != last_hosts:
            section_number += 1
            agenda_rows.append(("section", f"Section {section_number}  主持：{'、'.join(hosts)}"))
            last_hosts = hosts
        title_lines = len(wrap_text(measure_draw, event.get("title", ""), agenda_title_font, title_width))
        people_text = "\n".join(event.get("people", []))
        people_lines = len(wrap_text(measure_draw, people_text, agenda_people_font, people_width))
        lines = max(1, title_lines, people_lines)
        # 长主题和多人名单通过增加行高容纳，字号保持清晰可读。
        row_height = max(int(150 * scale), int((54 + lines * 50) * scale))
        agenda_rows.append(("event", event, row_height))
    agenda_height = int(165 * scale) + sum(int(105 * scale) if row[0] == "section" else row[2] for row in agenda_rows)
    header_height = int(1030 * scale)
    footer_height = int(300 * scale)
    calculated = header_height + role_height + agenda_height + footer_height + int(180 * scale)
    # 海报高度随实际专家和日程数量伸缩，避免短会议在模板比例下产生大段空白。
    height = calculated

    canvas = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(canvas)
    for y in range(height):
        ratio = y / max(1, height - 1)
        if ratio < .17:
            color = blend(light, (255, 255, 255), ratio / .17 * .45)
        elif ratio > .9:
            color = blend((255, 255, 255), light, (ratio - .9) / .1)
        else:
            color = blend((255, 255, 255), light, .08)
        draw.line((0, y, width, y), fill=color)
    # 模板经强模糊后仅保留纹理与色彩，避免旧海报文字残留。
    texture = ImageOps.fit(template, (width, height), Image.Resampling.LANCZOS).filter(ImageFilter.GaussianBlur(max(24, int(48 * scale))))
    canvas = Image.blend(canvas, texture, .10)
    draw = ImageDraw.Draw(canvas)
    # 内容直接绘制在模板背景上，不再添加外围白色圆角框和描边。

    agenda = model["agenda"]
    # 顶部机构信息只取自本次日程，模板原有左右标志不再保留。
    logo_size = int(430 * scale)
    logo = agenda_logo(agenda.get("path"), logo_size)
    if logo:
        logo_left, logo_top = int(55 * scale), int(28 * scale)
        canvas.paste(logo, (logo_left, logo_top), logo)
        organizer_label = agenda.get("organizer") or "主办单位待补充"
        compact_centered_text(draw, logo_left + logo_size + int(300 * scale), int(135 * scale),
                              organizer_label, font(38 * scale, True), dark,
                              int(560 * scale), int(4 * scale))
    y = int(410 * scale)
    centered_text(draw, (margin * 1.4, y, width - margin * 1.4, y + int(250 * scale)), agenda["title"], font(78 * scale, True), dark)
    y += int(270 * scale)
    organizer = agenda.get("organizer") or "待补充"
    centered_text(draw, (margin, y, width - margin, y + int(80 * scale)), "主办单位：" + organizer, font(30 * scale, True), dark)
    y += int(85 * scale)
    time_line = "会议时间：" + " ".join(value for value in (agenda.get("date"), agenda.get("meeting_time")) if value)
    centered_text(draw, (margin, y, width - margin, y + int(75 * scale)), time_line, font(31 * scale, True), dark)
    if agenda.get("meeting_code"):
        y += int(70 * scale)
        centered_text(draw, (margin, y, width - margin, y + int(60 * scale)), "腾讯会议：" + agenda["meeting_code"], font(27 * scale, True), dark)
    y = header_height

    for role, label, experts in grouped:
        y = draw_section_title(draw, y, width, margin, label, accent)
        columns = 3
        column_width = (width - margin * 2) / columns
        for index, expert in enumerate(experts):
            row, column = divmod(index, columns)
            row_start = row * columns
            row_count = min(columns, len(experts) - row_start)
            # 每一行围绕海报中心对称排列，单人和不足三人的末行不会靠左。
            center_x = width / 2 + (column - (row_count - 1) / 2) * column_width
            top = y + row * portrait_row
            ring = int(10 * scale)
            draw.ellipse((center_x - portrait_size / 2 - ring, top - ring,
                          center_x + portrait_size / 2 + ring, top + portrait_size + ring),
                         fill="white", outline=accent, width=max(3, int(6 * scale)))
            portrait = portrait_circle(expert["path"], portrait_size, (255, 255, 255))
            canvas.paste(portrait, (int(center_x - portrait_size / 2), int(top)), portrait)
            name_text = expert["name"] + "  " + (expert.get("display_title") or "教授")
            expert_font = font(48 * scale, True)
            caption_y = compact_centered_text(draw, center_x, top + portrait_size + int(14 * scale),
                                              name_text, expert_font, dark, column_width * .94,
                                              int(1 * scale))
            # 姓名职称与医院连续排版，行间距保持最小且字号完全一致。
            compact_centered_text(draw, center_x, caption_y + int(1 * scale),
                                  expert.get("hospital") or "单位待补充", expert_font, dark,
                                  column_width * .94, int(1 * scale))
        y += ((len(experts) + 2) // 3) * portrait_row

    y = draw_section_title(draw, y, width, margin, "会议日程", accent)
    table_left, table_right = margin, width - margin
    col1 = table_left + (table_right - table_left) * .18
    col2 = table_left + (table_right - table_left) * .67
    header_h = int(105 * scale)
    draw.rounded_rectangle((table_left, y, table_right, y + header_h), radius=int(12 * scale), fill=accent)
    for left, right, value in ((table_left, col1, "时间"), (col1, col2, "主题"), (col2, table_right, "嘉宾")):
        centered_text(draw, (left, y, right, y + header_h), value, font(38 * scale, True), "white")
    y += header_h
    for row in agenda_rows:
        if row[0] == "section":
            row_h = int(105 * scale)
            draw.rectangle((table_left, y, table_right, y + row_h), fill=blend(accent, (255, 255, 255), .13))
            centered_text(draw, (table_left, y, table_right, y + row_h), row[1], font(35 * scale, True), "white")
            y += row_h
            continue
        event, row_h = row[1], row[2]
        fill = (255, 255, 255) if int(y / max(1, row_h)) % 2 else blend(light, (255, 255, 255), .65)
        draw.rectangle((table_left, y, table_right, y + row_h), fill=fill)
        draw.line((table_left, y + row_h, table_right, y + row_h), fill=blend(accent, (255, 255, 255), .45), width=max(1, int(2 * scale)))
        centered_text(draw, (table_left + 8, y, col1 - 8, y + row_h), event["time"], font(34 * scale, True), dark)
        centered_text(draw, (col1 + 14, y, col2 - 14, y + row_h), event["title"], agenda_title_font, dark)
        expert_titles = {expert["name"]: expert.get("display_title", "教授") for expert in model["experts"]}
        names = "\n".join(name + " " + expert_titles.get(name, "") for name in event.get("people", []))
        centered_text(draw, (col2 + 12, y, table_right - 12, y + row_h), names, agenda_people_font, dark)
        y += row_h

    footer_y = height - int(175 * scale)
    centered_text(draw, (margin, footer_y, width - margin, height - int(40 * scale)),
                  "腾讯会议：" + (agenda.get("meeting_code") or "待补充"),
                  font(42 * scale, True), accent)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output, "PNG", optimize=True)
    return {"file": output.name, "width": width, "height": height, "experts": len(model["experts"]),
            "events": len(model["agenda"]["events"]),
            "metrics": {"generation_seconds": round(time.perf_counter() - started, 3), "ai_calls": 0}}


def poster_preview(path, size=(520, 1200)):
    with Image.open(path) as image:
        image.thumbnail(size, Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        image.convert("RGB").save(buffer, "JPEG", quality=86)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()
