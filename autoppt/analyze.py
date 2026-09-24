"""使用文本、位置及重复结构识别资料，不发送任何网络请求。"""

from __future__ import annotations

import hashlib
import json
import re
import statistics
import threading
import time
from collections import Counter
from pathlib import Path

from .ooxml import compact, read_deck, read_docx, read_package, read_wps, unpack_experts

TIME = re.compile(r"(\d{1,2}[:：]\d{2})\s*[-~至—–－]\s*(\d{1,2}[:：]\d{2})")
# 同时识别中文日期和模板中常见的点号、斜杠、短横线日期。
DATE = re.compile(r"(20\d{2})\s*(?:年\s*|[./-]\s*)(\d{1,2})\s*(?:月\s*|[./-]\s*)(\d{1,2})\s*日?")
ROLES = {"cover": "封面", "opening": "主席致辞", "chair": "主席简介", "host": "主持简介",
         "talk": "讲题", "speaker": "讲者简介", "discussion": "讨论名单", "guest": "嘉宾简介",
         "topics": "讨论话题", "summary": "会议总结", "ending": "结束页", "unknown": "待识别"}
PERSON_ROLES = {"chair", "host", "speaker", "guest"}
EVENT_ROLES = {"opening", "talk", "discussion", "summary"}
ROLE_TEXTS = {
    "chair": ("大会主席", "会议主席", "学术主席"),
    "host": ("大会主持", "会议主持", "环节主持", "主持人"),
    "speaker": ("大会讲者", "会议讲者", "讲者简介", "主讲嘉宾"),
    "guest": ("讨论嘉宾", "讨论专家", "特邀嘉宾", "与会专家"),
}
ENDING_GENERIC = ("会议结束", "大会结束", "谢谢", "感谢", "身体健康", "二维码", "扫码", "签到")
OCR_LOCK = threading.Lock()
OCR_ENGINE = None


def issue(code, message, level="warning", **extra):
    return {"code": code, "message": message, "level": level, **extra}


def normalized_text(text):
    # 中文间的排版空白可清理，英文内部的空格保持原样。
    return re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", text.strip())


def filename_name(path):
    value = Path(path).stem
    value = re.sub(r"[（(][^）)]*[）)]", "", value)
    # 解压序号、资料排序号和月日经常连续出现在姓名前，需依次清理。
    value = re.sub(r"^\d{3}_", "", value)
    value = re.sub(r"^(?:\d+[-_\s]+)+", "", value)
    value = re.sub(r"^(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])", "", value)
    # 角色词可能紧贴姓名，不能将讲者或讨论误识别为专家姓名。
    value = re.sub(r"专家简介|个人简介|个人简历|讲课|简介|简历|20\d{2}", " ", value)
    value = re.sub(r"讨论嘉宾|会议主持|大会主持|会议主席|大会主席|主持|讲者|讨论|嘉宾|主席", " ", value)
    candidates = re.findall(r"[\u4e00-\u9fff·]{2,6}", value)
    return candidates[0] if candidates else ""


def names_in(text, known=()):
    value = compact(text)
    matches = []
    for name in known:
        for hit in re.finditer(re.escape(compact(name)), value):
            matches.append((hit.start(), name))
    # 用展示称呼补充缺少简介的人员，避免把模板旧人物补进日程。
    for hit in re.finditer(r"([\u4e00-\u9fff]{2,4})(?:教授|主任医师|主治医师|医生)", value):
        candidate = hit.group(1)
        if not any(hit.start() <= start < hit.end() for start, _ in matches):
            candidate = re.sub(r"^(?:医院|附院|主持|主席|讲者)", "", candidate)
            if candidate.startswith('院') and len(candidate) >= 3:
                candidate = candidate[1:]
            if 2 <= len(candidate) <= 4:
                matches.append((hit.start(), candidate))
    return list(dict.fromkeys(name for _, name in sorted(matches)))


def hospital_in(text):
    value = normalized_text(text)
    hit = re.search(r"([\u4e00-\u9fffA-Za-z]{2,40}?医院(?:[\u4e00-\u9fff]{2,5}医院)?)", value)
    return hit.group(1) if hit else ""


def people_details(text, names):
    value = compact(text)
    result = {}
    for i, name in enumerate(names):
        start = value.find(compact(name))
        if start < 0:
            continue
        end = min([pos for other in names if other != name
                   for pos in [value.find(compact(other), start + len(name))] if pos >= 0] or [len(value)])
        tail = value[start + len(name):end]
        title = re.match(r"(教授|主任医师|主治医师|医生)", tail)
        if title:
            tail = tail[title.end():]
        result[name] = {"hospital": hospital_in(tail), "display_title": title.group(1) if title else ""}
    return result


def image_title(path, deck):
    """文字标题缺失时识别页面顶部横幅图片中的会议名称。"""
    global OCR_ENGINE
    candidates = [shape for slide in deck["slides"] for shape in slide["shapes"]
                  if shape.get("image") and shape["bbox"][1] < deck["height"] * .35
                  and shape["bbox"][2] > deck["width"] * .4
                  and shape["bbox"][3] > deck["height"] * .08]
    if not candidates:
        return ""
    try:
        from rapidocr import RapidOCR
    except ImportError:
        return ""
    package = read_package(path)
    lines = []
    with OCR_LOCK:
        if OCR_ENGINE is None:
            OCR_ENGINE = RapidOCR()
        for shape in sorted(candidates, key=lambda item: (item["bbox"][1], item["bbox"][0])):
            data = package.get(shape["image"], b"")
            if not data:
                continue
            result = OCR_ENGINE(data)
            if result is None or result.txts is None:
                continue
            for value, score in zip(result.txts, result.scores):
                value = normalized_text(value)
                marker = compact(value).upper()
                # 日程标签和页眉字段不属于会议名称，识别后直接过滤。
                if float(score) < .65 or marker in {"会议日程", "大会日程", "日程", "AGENDA"}:
                    continue
                if re.search(r"会议(?:时间|地点|地址|主席)|主办单位|腾讯会议", value):
                    continue
                if len(marker) >= 4:
                    lines.append(value)
    return "".join(lines)


def read_expert(path):
    path = Path(path)
    name = filename_name(path)
    candidates, lines, warnings = [], [], []
    if path.suffix.lower() == ".docx":
        lines, candidates = read_docx(path)
        for index, candidate in enumerate(candidates):
            width, height = candidate.get("width", 0), candidate.get("height", 0)
            ratio = width / max(height, 1)
            # Word中常见的细横线也是图片，按比例和有效尺寸优先保留人物照片。
            score = (2 if .38 < ratio < 1.45 else 0) + (1 if min(width, height) >= 80 else 0)
            if min(width, height) < 16 or ratio < .15 or ratio > 4:
                score -= 4
            candidate.update(score=score, id=str(index))
    elif path.suffix.lower() == ".wps":
        lines, candidates = read_wps(path)
        for index, candidate in enumerate(candidates):
            candidate.update(score=1 if len(candidates) == 1 else .5, id=str(index))
    else:
        deck = read_deck(path)
        for slide in deck["slides"]:
            # 几何排序用于普通简介，表格保持行列对应关系。
            for shape in sorted(slide["shapes"], key=lambda s: (s["bbox"][1], s["bbox"][0])):
                if shape["table"]:
                    lines.extend(" ".join(c for c in row if c) for row in shape["table"])
                else:
                    lines.extend(shape["lines"])
                # 同时识别独立图片和普通形状的图片填充，兼容WPS与PowerPoint的不同保存方式。
                if shape.get("image") and shape.get("thumbnail"):
                    x, y, w, h = shape["bbox"]
                    ratio = w / max(h, 1)
                    area = w * h / (deck["width"] * deck["height"])
                    score = (2 if .38 < ratio < 1.45 else 0) + (1 if .025 < area < .55 else 0)
                    if area > .65 or h < deck["height"] * .15:
                        score -= 4
                    candidates.append({"id": f'{slide["number"]}:{shape["id"]}', "image": shape["image"],
                                       "thumbnail": shape["thumbnail"], "score": score,
                                       "rotation": shape.get("rotation", 0),
                                       "flip_h": shape.get("flip_h", False),
                                       "flip_v": shape.get("flip_v", False)})
    candidates.sort(key=lambda i: i["score"], reverse=True)
    if not name:
        found = names_in("\n".join(lines))
        name = found[0] if len(found) == 1 else ""
    if not name:
        warnings.append(issue("expert_name", f"无法确定专家姓名：{path.name}", "error"))
    cleaned = []
    for line in lines:
        line = normalized_text(line)
        value = compact(line)
        if not value or value.isdigit() or value in ("总体情况", "个人简介", "专家简介", "社会兼职:", "社会兼职："):
            continue
        if value in {name, name + "教授", name + "医生", name + "个人简介", name + "简介"}:
            continue
        if value.startswith("单击此处"):
            continue
        if name and line.startswith(name):
            line = re.sub(r"^[\s，,：:]+", "", line[len(name):])
            line = re.sub(r"^教授\s*", "", line)
        if line and line not in cleaned:
            cleaned.append(line)
    hospital = next((hospital_in(line) for line in cleaned if hospital_in(line)), "")
    body_has_name = bool(name and name in compact("\n".join(lines)))
    if name and not body_has_name:
        warnings.append(issue("name_from_filename", f"{name}的姓名取自文件名，正文未出现姓名", "info", expert=name))
    ambiguous = len(candidates) > 1 and candidates[0]["score"] <= candidates[1]["score"]
    if ambiguous:
        warnings.append(issue("photo_ambiguous", f"{name}有多张候选照片，请选择", "error", expert=name))
    if not candidates:
        warnings.append(issue("photo_missing", f"{name}未找到可读取照片", "warning", expert=name))
    return {"name": name, "hospital": hospital, "bio": cleaned, "path": str(path.resolve()),
            "source": path.name, "photo": candidates[0]["image"] if candidates else "",
            "photos": candidates, "photo_confirmed": not ambiguous, "issues": warnings}


def classify_event(content):
    value = compact(content)
    if "致辞" in value or "开场" in value:
        return "opening"
    if "总结" in value or "闭幕" in value:
        return "summary"
    if "讨论" in value or "交流环节" in value:
        return "discussion"
    return "talk"


def parse_agenda(path, experts):
    deck = read_deck(path)
    known = [e["name"] for e in experts if e["name"]]
    all_shapes = [s for slide in deck["slides"] for s in slide["shapes"]]
    text = "\n".join(s["text"] for s in all_shapes)
    found_names = names_in(text, known)
    names = list(dict.fromkeys(known + found_names))
    info = {"title": "", "date": "", "venue": "", "organizer": "", "chairs": [], "events": [],
            "people": {}, "source": Path(path).name, "issues": []}
    date = DATE.search(text)
    if date:
        info["date"] = f"{date[1]}年{int(date[2])}月{int(date[3])}日"
    for shape in all_shapes:
        value = shape["text"]
        if "主持" in value and not shape["table"]:
            host_names = names_in(value, names)
            info["people"].update(people_details(value, host_names))
        if "会议主席" in value or "大会主席：" in value:
            info["chairs"] = names_in(value, names)
        for key, pattern in [("venue", r"会议(?:地点|地址)\s*[:：]\s*(.+)"),
                             ("organizer", r"主办单位\s*[:：]\s*(.+)")]:
            hit = re.search(pattern, value)
            if hit:
                info[key] = hit[1].strip()
    title_shapes = [s for s in all_shapes if not s["table"] and s["text"] and s["font"] >= 25
                    and "日程" not in compact(s["text"]) and "会议时间" not in s["text"]
                    and not names_in(s["text"], names)]
    if title_shapes:
        # 同行分散标题可能由多个文本框组成，优先按横向阅读顺序组合。
        title_shapes.sort(key=lambda s: (round(s["bbox"][1] / max(deck["height"] * .1, 1)), s["bbox"][0]))
        if len(title_shapes) > 1 and all(s["bbox"][1] < deck["height"] * .25 for s in title_shapes):
            title_shapes.sort(key=lambda s: s["bbox"][0])
        info["title"] = "".join(compact(s["text"]) for s in title_shapes)
    if not info["title"]:
        # 有些日程把整段标题转成图片，使用本地OCR补充会议名称。
        info["title"] = image_title(path, deck)
    current_hosts = []
    for slide in deck["slides"]:
        tables = [s for s in slide["shapes"] if s["table"] and any(TIME.search(compact("".join(r))) for r in s["table"])]
        if tables:
            for table in tables:
                for row in table["table"]:
                    merged = " ".join(row)
                    if "主持" in merged and not TIME.search(compact(merged)):
                        current_hosts = names_in(merged, names)
                        info["people"].update(people_details(merged, current_hosts))
                        continue
                    match = TIME.search(compact(row[0])) if row else None
                    if not match:
                        continue
                    content = normalized_text(row[1]) if len(row) > 1 else ""
                    speakers = names_in(row[2], names) if len(row) > 2 else []
                    kind = classify_event(content)
                    info["events"].append({"time": match[0], "kind": kind, "title": content, "people": speakers,
                                           "hosts": current_hosts[:] if kind in ("talk", "discussion") else []})
                    if len(row) > 2:
                        info["people"].update(people_details(row[2], speakers))
        else:
            shapes = [s for s in slide["shapes"] if s["text"]]
            time_shapes = [s for s in shapes if TIME.fullmatch(compact(s["text"]))]
            host_shapes = [s for s in shapes if "主持" in s["text"]]
            times = sorted(time_shapes, key=lambda s: s["bbox"][1] + s["bbox"][3] / 2)
            for index, ts in enumerate(times):
                x, y, w, h = ts["bbox"]
                center = y + h / 2
                hosts = [s for s in host_shapes if s["bbox"][1] < y]
                if hosts:
                    current_hosts = names_in(max(hosts, key=lambda s: s["bbox"][1])["text"], names)
                candidates = []
                for shape in shapes:
                    sx, sy, sw, sh = shape["bbox"]
                    if shape in time_shapes or shape in host_shapes or sx < x + w * .8:
                        continue
                    mid = sy + sh / 2
                    nearest = min(range(len(times)), key=lambda j: abs(mid - (times[j]["bbox"][1] + times[j]["bbox"][3] / 2)))
                    if nearest == index and abs(mid - center) < deck["height"] * .07:
                        candidates.append(shape)
                people_shapes = [s for s in candidates if names_in(s["text"], names)]
                content_shapes = [s for s in candidates if s not in people_shapes and "姓氏排序" not in s["text"]]
                content = " ".join(s["text"] for s in sorted(content_shapes, key=lambda s: s["bbox"][0]))
                persons = list(dict.fromkeys(n for s in people_shapes for n in names_in(s["text"], names)))
                kind = classify_event(content)
                info["events"].append({"time": compact(ts["text"]), "kind": kind, "title": content,
                                       "people": persons, "hosts": current_hosts[:] if kind in ("talk", "discussion") else []})
                for shape in people_shapes:
                    info["people"].update(people_details(shape["text"], persons))
    if not info["chairs"]:
        info["chairs"] = next((e["people"] for e in info["events"] if e["kind"] == "opening"), [])
    if not info["events"]:
        info["issues"].append(issue("agenda_empty", "日程未识别出时间段，请在日程编辑区补充", "error"))
    for event in info["events"]:
        if not event["people"] or not event["title"]:
            info["issues"].append(issue("event_incomplete", f"{event['time']}的人员或内容不完整", "error"))
    return info


def classify_slide(slide, repeated_titles, total_slides=None, deck_width=0, deck_height=0):
    texts = [compact(s["text"]) for s in slide["shapes"] if s["text"]]
    joined = "\n".join(texts)
    for word, role in [("讨论话题", "topics"), ("讨论问题", "topics"),
                       ("会议结束", "ending"), ("感谢聆听", "ending"), ("感谢观看", "ending"),
                       ("主席致辞", "opening"), ("开场致辞", "opening"), ("会议致辞", "opening"),
                       ("会议总结", "summary"), ("大会总结", "summary"), ("总结致辞", "summary")]:
        if any(word == t or word in t for t in texts):
            return role
    deck_area = deck_width * deck_height
    has_portrait = any(
        shape.get("image")
        and shape["bbox"][1] > deck_height * .1
        and shape["bbox"][3] > deck_height * .18
        and .25 < shape["bbox"][2] / max(1, shape["bbox"][3]) < 1.7
        and (not deck_area or shape["bbox"][2] * shape["bbox"][3] < deck_area * .55)
        for shape in slide["shapes"]
    )
    has_bio = any(len(compact(shape["text"])) >= 45 for shape in slide["shapes"] if shape["text"])
    if has_portrait or has_bio:
        for role, markers in ROLE_TEXTS.items():
            if any(marker in text for marker in markers for text in texts):
                return role
    if any(any(word in t for word in ("讨论环节", "讨论交流", "交流讨论", "互动讨论", "互动交流", "专家讨论"))
           for t in texts):
        return "discussion"
    if slide["number"] == 1:
        return "cover"
    if total_slides and slide["number"] == total_slides and len(joined) < 80:
        return "ending"
    if DATE.search(joined) and not any(len(t) > 120 for t in texts):
        return "cover"
    # 含人物照片或长简介的页面按通用专家页处理，具体角色由生成日程决定。
    if has_portrait and (has_bio or len(texts) >= 2):
        return "guest"
    meaningful = [t for t in texts if t not in repeated_titles and "教授" not in t and len(t) > 5]
    if "教授" in joined and meaningful and max(map(len, meaningful)) < 160:
        return "talk"
    # 标题页可能使用主任医师、医生等称呼，或只保留一个大字号标题框。
    if any(re.search(r"教授|主任医师|主治医师|医生|医院", t) for t in texts) and meaningful:
        return "talk"
    if any(shape["font"] >= 24 and shape["text"] for shape in slide["shapes"]):
        return "talk"
    return "unknown"


def infer_slide_fields(slide, role, repeated_titles=(), existing=None):
    """根据文字、图片和几何位置补全当前用途所需的模板区域。"""
    shapes = slide["shapes"]
    shape_ids = {shape["id"] for shape in shapes}
    fields = {}
    for key, value in (existing or {}).items():
        values = value if isinstance(value, list) else [value]
        valid = [str(item) for item in values if str(item) in shape_ids]
        if key in ("meeting", "metadata", "ending_content"):
            fields[key] = valid
        elif valid:
            fields[key] = valid[0]
    texts = [shape for shape in shapes if shape["kind"] == "sp" and shape["text"]]
    shared = [shape["id"] for shape in texts if compact(shape["text"]) in repeated_titles]
    fields["meeting"] = list(dict.fromkeys(fields.get("meeting", []) + shared))

    if role in PERSON_ROLES:
        markers = {compact(value) for values in ROLE_TEXTS.values() for value in values}
        role_shapes = [shape for shape in texts if any(marker in compact(shape["text"]) for marker in markers)
                       and len(compact(shape["text"])) <= 16]
        if not fields.get("role") and role_shapes:
            fields["role"] = max(role_shapes, key=lambda shape: shape["font"])["id"]
        excluded = set(fields.get("meeting", [])) | {shape["id"] for shape in role_shapes}
        remaining = [shape for shape in texts if shape["id"] not in excluded]
        if not fields.get("bio") and remaining:
            body_candidates = [shape for shape in remaining if len(compact(shape["text"])) >= 18]
            if body_candidates:
                fields["bio"] = max(body_candidates, key=lambda shape: (len(compact(shape["text"])), shape["bbox"][2] * shape["bbox"][3]))["id"]
        identity_pool = [shape for shape in remaining if shape["id"] != fields.get("bio")]
        if not fields.get("identity") and identity_pool:
            fields["identity"] = max(identity_pool, key=lambda shape: (
                bool(names_in(shape["text"])),
                not bool(hospital_in(shape["text"])),
                shape["font"],
                -len(compact(shape["text"])),
            ))["id"]
        if not fields.get("hospital"):
            hospitals = [shape for shape in identity_pool if shape["id"] != fields.get("identity") and hospital_in(shape["text"])]
            if hospitals:
                fields["hospital"] = max(hospitals, key=lambda shape: shape["font"])["id"]
        if not fields.get("photo"):
            photos = [shape for shape in shapes if shape.get("image") and shape["bbox"][3] > 0
                      and shape["bbox"][1] > slide.get("deck_height", 0) * .1
                      and shape["bbox"][3] > slide.get("deck_height", 0) * .18
                      and .18 < shape["bbox"][2] / max(1, shape["bbox"][3]) < 2.2
                      and shape["bbox"][2] * shape["bbox"][3] < slide.get("deck_area", float("inf")) * .7]
            if photos:
                fields["photo"] = max(photos, key=lambda shape: shape["bbox"][2] * shape["bbox"][3])["id"]
    elif role in EVENT_ROLES:
        remaining = [shape for shape in texts if shape["id"] not in set(fields.get("meeting", []))]
        attendees = [shape for shape in remaining if names_in(shape["text"]) or "{{people}}" in shape["text"]
                     or re.search(r"教授|主任医师|主治医师|医生|医院", shape["text"])]
        if not fields.get("people") and attendees:
            fields["people"] = max(attendees, key=lambda shape: (len(compact(shape["text"])), shape["bbox"][1]))["id"]
        title_pool = [shape for shape in remaining if shape["id"] != fields.get("people")]
        if not fields.get("title") and title_pool:
            fields["title"] = max(title_pool, key=lambda shape: (shape["font"], shape["bbox"][2], -shape["bbox"][1]))["id"]
    elif role == "cover":
        remaining = [shape for shape in texts if shape["id"] not in set(fields.get("meeting", []))]
        choices = [shape for shape in remaining if not DATE.search(shape["text"]) and "主办" not in shape["text"]]
        if not choices:
            choices = [shape for shape in texts if not DATE.search(shape["text"]) and "主办" not in shape["text"]]
        if not fields.get("title") and choices:
            fields["title"] = max(choices, key=lambda shape: (shape["font"], shape["bbox"][2]))["id"]
        metadata = [shape["id"] for shape in remaining if DATE.search(shape["text"]) or "主办" in shape["text"]]
        fields["metadata"] = list(dict.fromkeys(fields.get("metadata", []) + metadata))
    elif role == "ending":
        # 结束页常带模板旧会议名称和日期，将这些对象单独标记以便生成时替换或清空。
        candidates = []
        for shape in texts:
            value = compact(shape["text"])
            is_generic = any(word in value for word in ENDING_GENERIC)
            is_meeting_title = any(word in value for word in ("会议", "交流会", "研讨会", "学术", "沙龙"))
            if DATE.search(shape["text"]) or (is_meeting_title and not is_generic):
                candidates.append(shape["id"])
        fields["ending_content"] = list(dict.fromkeys(
            fields.get("ending_content", []) + fields.get("meeting", []) + candidates))
    return fields


def analyze_template(path):
    deck = read_deck(path)
    counts = Counter(compact(s["text"]) for slide in deck["slides"] for s in slide["shapes"]
                     if s["text"] and s["bbox"][1] < deck["height"] * .2)
    repeated = {text for text, count in counts.items() if count >= 3 and len(text) > 8}
    issues, slides = [], []
    for slide in deck["slides"]:
        role = classify_slide(slide, repeated, len(deck["slides"]), deck["width"], deck["height"])
        shapes = slide["shapes"]
        enriched = {**slide, "deck_area": deck["width"] * deck["height"], "deck_height": deck["height"]}
        fields = infer_slide_fields(enriched, role, repeated)
        required = (("bio", "identity", "photo") if role in PERSON_ROLES else
                    ("people", "title") if role in EVENT_ROLES else
                    ("title",) if role == "cover" else ())
        missing = [field for field in required if not fields.get(field)]
        if missing:
            issues.append(issue("template_fields", f"模板第{slide['number']}页缺少可自动填充区域，生成时将保留空白或原布局",
                                "warning", slide=slide["number"], fields=missing))
        if role == "unknown":
            issues.append(issue("template_unknown", f"模板第{slide['number']}页用途未识别，生成时仅在没有合适页面时备用", "warning", slide=slide["number"]))
        if role == "topics":
            issues.append(issue("topic_policy", f"模板第{slide['number']}页包含讨论话题，默认排除，可选择每场保留", "warning", slide=slide["number"]))
        slides.append({**slide, "role": role, "fields": fields})
    return {**deck, "slides": slides, "issues": issues, "fingerprint": hashlib.sha256(Path(path).read_bytes()).hexdigest()}


def discover(folder):
    root = Path(folder)
    decks = [p for p in root.glob("*.pptx") if not p.name.startswith("~$")]
    agenda = [p for p in decks if "日程" in p.name or "agenda" in p.name.lower()]
    template = [p for p in decks if p not in agenda and any(t in p.name.lower() for t in ("模板", "模版", "串场", "template"))]
    if len(agenda) != 1 or len(template) != 1:
        raise ValueError("目录中无法唯一确定日程和模板，请使用显式文件参数或网页上传")
    experts = [p for p in root.rglob("*") if p.suffix.lower() in (".pptx", ".docx", ".wps") and p not in [agenda[0], template[0]] and not p.name.startswith("~$")]
    if not experts:
        experts = list(root.glob("*.zip"))
    return template[0], agenda[0], experts


def analyze(template, agenda, sources, workdir, cache_dir=None):
    started = time.perf_counter()
    expanded = []
    for i, path in enumerate(sources):
        if Path(path).suffix.lower() == ".zip":
            expanded.extend(unpack_experts(path, Path(workdir) / f"unpacked_{i}"))
        elif Path(path).suffix.lower() in (".pptx", ".docx", ".wps"):
            expanded.append(Path(path))
        else:
            raise ValueError(f"暂不支持该专家文件格式：{Path(path).name}")
    experts, seen, issues = [], set(), []
    for path in expanded:
        checksum = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        if checksum in seen:
            continue
        seen.add(checksum)
        expert = read_expert(path)
        if expert["name"] in [e["name"] for e in experts]:
            issues.append(issue("duplicate_name", f"{expert['name']}存在内容不同的多个简介，请删除重复或修改姓名", "error"))
        experts.append(expert)
    schedule = parse_agenda(agenda, experts)
    profile = analyze_template(template)
    if cache_dir:
        saved = Path(cache_dir) / (profile['fingerprint'] + '.json')
        if saved.exists():
            try:
                bindings = json.loads(saved.read_text(encoding='utf-8'))
                if len(bindings) == len(profile['slides']):
                    for slide, binding in zip(profile['slides'], bindings):
                        if binding.get('role') in ROLES:
                            slide['role'] = binding['role']
                            slide['fields'] = binding['fields']
                    profile['cache_used'] = True
            except (ValueError, KeyError, TypeError):
                profile['cache_used'] = False
    for event in schedule["events"]:
        for name in event["people"] + event["hosts"]:
            if name not in [e["name"] for e in experts]:
                message = f"日程中的{name}缺少独立专家简介"
                if not any(i["message"] == message for i in issues):
                    issues.append(issue("missing_expert", message, "error", expert=name))
    for expert in experts:
        issues.extend(expert.pop("issues"))
        event_hospital = schedule["people"].get(expert["name"], {}).get("hospital", "")
        if event_hospital and expert["hospital"] and compact(event_hospital) != compact(expert["hospital"]):
            issues.append(issue("hospital_difference", f"{expert['name']}的日程单位与简介不同，展示单位默认采用日程，简介保留原文", "warning", expert=expert["name"]))
    issues.extend(schedule.pop("issues"))
    issues.extend(profile.pop("issues"))
    # 网页默认保留主席简介并允许待核对版本，讨论话题页默认关闭。
    return {"version": 1, "experts": experts, "agenda": schedule, "template": profile, "issues": issues,
            "options": {"draft": True, "repeat_chairs": True, "include_topics": False},
            "metrics": {"analysis_seconds": round(time.perf_counter() - started, 3), "ai_calls": 0}}
