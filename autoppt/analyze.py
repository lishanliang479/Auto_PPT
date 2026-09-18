"""使用文本、位置及重复结构识别资料，不发送任何网络请求。"""

from __future__ import annotations

import hashlib
import json
import re
import statistics
import time
from collections import Counter
from pathlib import Path

from .ooxml import compact, read_deck, read_docx, unpack_experts

TIME = re.compile(r"(\d{1,2}[:：]\d{2})\s*[-~至—–－]\s*(\d{1,2}[:：]\d{2})")
DATE = re.compile(r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
ROLES = {"cover": "封面", "opening": "主席致辞", "chair": "主席简介", "host": "主持简介",
         "talk": "讲题", "speaker": "讲者简介", "discussion": "讨论名单", "guest": "嘉宾简介",
         "topics": "讨论话题", "summary": "会议总结", "ending": "结束页", "unknown": "待识别"}


def issue(code, message, level="warning", **extra):
    return {"code": code, "message": message, "level": level, **extra}


def normalized_text(text):
    # 中文间的排版空白可清理，英文内部的空格保持原样。
    return re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", text.strip())


def filename_name(path):
    value = Path(path).stem
    value = re.sub(r"[（(][^）)]*[）)]", "", value)
    value = re.sub(r"^(?:\d+[-_\s]+)+", "", value)
    value = re.split(r"专家简介|个人简介|个人简历|讲课|简介|简历|20\d{2}", value)[0]
    value = re.split(r"[-_\s]+", value.strip())[-1]
    return value if re.fullmatch(r"[\u4e00-\u9fff·]{2,6}", value) else ""


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


def read_expert(path):
    path = Path(path)
    name = filename_name(path)
    candidates, lines, warnings = [], [], []
    if path.suffix.lower() == ".docx":
        lines, candidates = read_docx(path)
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
                if shape["kind"] == "pic" and shape.get("thumbnail"):
                    x, y, w, h = shape["bbox"]
                    ratio = w / max(h, 1)
                    area = w * h / (deck["width"] * deck["height"])
                    score = (2 if .38 < ratio < 1.45 else 0) + (1 if .025 < area < .55 else 0)
                    if area > .65 or h < deck["height"] * .15:
                        score -= 4
                    candidates.append({"id": f'{slide["number"]}:{shape["id"]}', "image": shape["image"],
                                       "thumbnail": shape["thumbnail"], "score": score})
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


def classify_slide(slide, repeated_titles):
    texts = [compact(s["text"]) for s in slide["shapes"] if s["text"]]
    joined = "\n".join(texts)
    for word, role in [("讨论话题", "topics"), ("会议结束", "ending"), ("感谢聆听", "ending"),
                       ("主席致辞", "opening"), ("开场致辞", "opening"), ("会议总结", "summary")]:
        if any(word == t or (role == "ending" and word in t) for t in texts):
            return role
    for word, role in [("大会主席", "chair"), ("会议主席", "chair"), ("大会主持", "host"),
                       ("环节主持", "host"), ("大会讲者", "speaker"), ("讲者简介", "speaker"),
                       ("讨论嘉宾", "guest"), ("讨论专家", "guest")]:
        if word in texts:
            return role
    if any(t in ("讨论环节", "讨论交流", "交流讨论") for t in texts):
        return "discussion"
    if DATE.search(joined) and not any(len(t) > 120 for t in texts):
        return "cover"
    meaningful = [t for t in texts if t not in repeated_titles and "教授" not in t and len(t) > 5]
    if "教授" in joined and meaningful and max(map(len, meaningful)) < 160:
        return "talk"
    if slide["number"] == 1 and any("会" in t or "论坛" in t for t in texts):
        return "cover"
    return "unknown"


def analyze_template(path):
    deck = read_deck(path)
    counts = Counter(compact(s["text"]) for slide in deck["slides"] for s in slide["shapes"]
                     if s["text"] and s["bbox"][1] < deck["height"] * .2)
    repeated = {text for text, count in counts.items() if count >= 3 and len(text) > 8}
    issues, slides = [], []
    for slide in deck["slides"]:
        role = classify_slide(slide, repeated)
        shapes = slide["shapes"]
        texts = [s for s in shapes if s["text"]]
        fields = {}
        shared = [s["id"] for s in texts if compact(s["text"]) in repeated]
        fields["meeting"] = shared
        if role in ("chair", "host", "speaker", "guest"):
            role_shapes = [s for s in texts if compact(s["text"]) in ("大会主席", "会议主席", "大会主持", "环节主持", "大会讲者", "讲者简介", "讨论嘉宾", "讨论专家")]
            if role_shapes:
                fields["role"] = role_shapes[0]["id"]
            remaining = [s for s in texts if s["id"] not in shared and s not in role_shapes]
            if remaining:
                body = max(remaining, key=lambda s: len(s["text"]))
                fields["bio"] = body["id"]
                identities = [s for s in remaining if s != body]
                if identities:
                    identity = max(identities, key=lambda s: (bool(names_in(s["text"])), s["font"]))
                    fields["identity"] = identity["id"]
                    hospitals = [s for s in identities if s != identity and hospital_in(s["text"])]
                    if hospitals:
                        fields["hospital"] = hospitals[0]["id"]
            photos = [s for s in shapes if s["kind"] == "pic" and s["bbox"][1] > deck["height"] * .15
                      and s["bbox"][3] > deck["height"] * .2
                      and .25 < s["bbox"][2] / max(1, s["bbox"][3]) < 1.6
                      and s["bbox"][2] * s["bbox"][3] < deck["width"] * deck["height"] * .55]
            if photos:
                fields["photo"] = max(photos, key=lambda s: s["bbox"][2] * s["bbox"][3])["id"]
            if any(k not in fields for k in ("bio", "identity", "photo")):
                issues.append(issue("template_fields", f"模板第{slide['number']}页需要确认简介、姓名或照片区域", "error", slide=slide["number"]))
        elif role in ("opening", "summary", "discussion", "talk"):
            remaining = [s for s in texts if s["id"] not in shared]
            attendees = [s for s in remaining if names_in(s["text"]) or "{{people}}" in s["text"]]
            if attendees:
                fields["people"] = max(attendees, key=lambda s: len(s["text"]))["id"]
            titles = [s for s in remaining if s not in attendees]
            if titles:
                fields["title"] = max(titles, key=lambda s: s["font"])["id"]
            if not fields.get("people") or not fields.get("title"):
                issues.append(issue("template_fields", f"模板第{slide['number']}页需要确认标题或名单区域", "error", slide=slide["number"]))
        elif role == "cover":
            choices = [s for s in texts if not DATE.search(s["text"]) and "主办" not in s["text"]]
            if choices:
                fields["title"] = max(choices, key=lambda s: s["font"])["id"]
            fields["metadata"] = [s["id"] for s in texts if DATE.search(s["text"]) or "主办单位" in s["text"]]
        elif role == "unknown":
            issues.append(issue("template_unknown", f"模板第{slide['number']}页用途未识别，默认不加入输出", "warning", slide=slide["number"]))
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
    experts = [p for p in root.rglob("*") if p.suffix.lower() in (".pptx", ".docx") and p not in [agenda[0], template[0]] and not p.name.startswith("~$")]
    if not experts:
        experts = list(root.glob("*.zip"))
    return template[0], agenda[0], experts


def analyze(template, agenda, sources, workdir, cache_dir=None):
    started = time.perf_counter()
    expanded = []
    for i, path in enumerate(sources):
        if Path(path).suffix.lower() == ".zip":
            expanded.extend(unpack_experts(path, Path(workdir) / f"unpacked_{i}"))
        elif Path(path).suffix.lower() in (".pptx", ".docx"):
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
            "options": {"draft": True, "repeat_chairs": True, "include_topics": False, "bio_font_size": 16},
            "metrics": {"analysis_seconds": round(time.perf_counter() - started, 3), "ai_calls": 0}}
