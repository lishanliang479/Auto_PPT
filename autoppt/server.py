"""仅监听本机的网页工具，任务文件保存在项目work目录。"""

from __future__ import annotations

import copy
import json
import mimetypes
import secrets
import threading
import traceback
import urllib.parse
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .analyze import ROLES, analyze, discover
from .generate import generate, page_plan
from .ooxml import MAX_FILE, read_deck

ROOT = Path(__file__).resolve().parent.parent
JOBS = {}
LOCK = threading.Lock()
TOKEN = secrets.token_urlsafe(32)
MAX_UPLOAD = 400 * 1024 * 1024


def new_job():
    identifier = secrets.token_hex(12)
    directory = ROOT / "work" / identifier
    directory.mkdir(parents=True)
    job = {"id": identifier, "status": "analyzing", "message": "正在读取资料和分析模板", "directory": directory}
    with LOCK:
        JOBS[identifier] = job
    return job


def analyze_job(job, template, agenda, experts):
    try:
        model = analyze(template, agenda, experts, job["directory"], ROOT / 'work/template_profiles')
        job["model"] = model
        job["status"] = "ready"
        job["message"] = "识别完成，可核对并生成"
        (job["directory"] / "analysis.json").write_text(json.dumps(model, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        job.update(status="failed", message=str(exc))
        traceback.print_exc()


def apply_edits(original, incoming):
    """只接收可编辑的业务字段，文件路径和图片来源由服务端保管。"""
    model = copy.deepcopy(original)
    if not isinstance(incoming, dict):
        raise ValueError("提交内容格式不正确")
    for key in ("title", "date", "venue", "organizer"):
        if key in incoming.get("agenda", {}):
            model["agenda"][key] = str(incoming["agenda"][key])[:2000]
    if "events" in incoming.get("agenda", {}):
        events = incoming["agenda"]["events"]
        if not isinstance(events, list) or len(events) > 200:
            raise ValueError("日程数量超出限制")
        model["agenda"]["events"] = []
        for event in events:
            if not all(isinstance(event.get(k), str) for k in ("time", "kind", "title")):
                raise ValueError("日程字段格式不正确")
            for key in ("people", "hosts"):
                if not isinstance(event.get(key), list) or any(not isinstance(n, str) or len(n) > 60 for n in event[key]):
                    raise ValueError("日程人员格式不正确")
            model["agenda"]["events"].append({k: event[k] for k in ("time", "kind", "title", "people", "hosts")})
    updates = incoming.get("experts", [])
    if len(updates) != len(model["experts"]):
        raise ValueError("专家数量发生变化，请重新上传资料")
    for expert, update in zip(model["experts"], updates):
        if 'photo_confirmed' in update:
            expert['photo_confirmed'] = bool(update['photo_confirmed'])
        for key in ("name", "hospital", "photo"):
            if key in update:
                expert[key] = str(update[key])[:500]
        if "bio" in update:
            if not isinstance(update["bio"], list) or len(update["bio"]) > 500 or any(not isinstance(x, str) for x in update["bio"]):
                raise ValueError("简介条目格式不正确")
            expert["bio"] = [x[:10000] for x in update["bio"] if x.strip()]
    slides = incoming.get("template", {}).get("slides", [])
    if len(slides) != len(model["template"]["slides"]):
        raise ValueError("模板页面数量发生变化")
    allowed = {"meeting", "metadata", "role", "bio", "identity", "photo", "hospital", "people", "title"}
    for slide, update in zip(model["template"]["slides"], slides):
        if update.get("role") not in ROLES:
            raise ValueError("页面角色无效")
        slide["role"] = update["role"]
        ids = {s["id"] for s in slide["shapes"]}
        fields = {}
        for key, value in update.get("fields", {}).items():
            if key not in allowed:
                continue
            values = value if isinstance(value, list) else [value]
            if any(v and v not in ids for v in values):
                raise ValueError("文本框或图片区域无效")
            fields[key] = [v for v in values if v] if key in ("meeting", "metadata") else (values[0] if values else "")
        slide["fields"] = fields
    for key in ("draft", "repeat_chairs", "include_topics"):
        if key in incoming.get("options", {}):
            model["options"][key] = bool(incoming["options"][key])
    # 展示单位与姓名修改同步到本次数据，原始资料文件保持不变。
    if "people" in incoming.get("agenda", {}):
        people = incoming["agenda"]["people"]
        if not isinstance(people, dict) or len(people) > 300:
            raise ValueError("人员展示信息格式不正确")
        model["agenda"]["people"] = {str(name)[:60]: {key: str(data.get(key, ""))[:500] for key in ("hospital", "display_title")}
                                      for name, data in people.items() if isinstance(data, dict)}
    return model


def generate_job(job, model):
    try:
        filename = "待核对会议串场.pptx" if model["options"]["draft"] else "会议串场.pptx"
        report = generate(model, job["directory"] / filename)
        cache = ROOT / 'work/template_profiles'
        cache.mkdir(parents=True, exist_ok=True)
        (cache / (model['template']['fingerprint'] + '.json')).write_text(
            json.dumps([{'role': s['role'], 'fields': s['fields']} for s in model['template']['slides']], ensure_ascii=False), encoding='utf-8')
        job["model"] = model
        job["report"] = report
        job["result"] = filename
        job["output_slides"] = read_deck(job["directory"] / filename)
        job.update(status="complete", message="PPT已生成，结构与人员内容校验通过")
    except Exception as exc:
        job.update(status="ready", message=str(exc), generation_error=str(exc))
        traceback.print_exc()


class Handler(BaseHTTPRequestHandler):
    server_version = "AutoPPT/1.0"

    def log_message(self, fmt, *args):
        return

    def response(self, data, status=200, mime="application/json; charset=utf-8", filename=None):
        if isinstance(data, (dict, list)):
            data = json.dumps(data, ensure_ascii=False).encode("utf-8")
        elif isinstance(data, str):
            data = data.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'")
        if filename:
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + urllib.parse.quote(filename))
        self.end_headers()
        self.wfile.write(data)

    def trusted_host(self):
        host = self.headers.get("Host", "").split(":")[0]
        return host in ("localhost", "127.0.0.1")

    def do_GET(self):
        if not self.trusted_host():
            return self.response({"error": "仅允许本机访问"}, 403)
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/config":
            samples = [n for n in ("input", "input1") if (ROOT / n).is_dir()]
            return self.response({"token": TOKEN, "roles": ROLES, "samples": samples})
        if path.startswith("/api/jobs/"):
            parts = path.strip("/").split("/")
            job = JOBS.get(parts[2])
            if not job:
                return self.response({"error": "任务不存在或服务已重启"}, 404)
            if len(parts) == 3:
                result = {k: v for k, v in job.items() if k in ("id", "status", "message", "model", "report", "generation_error", "output_slides")}
                return self.response(result)
            if parts[3] == "download" and job.get("result"):
                file = job["directory"] / job["result"]
                return self.response(file.read_bytes(), mime="application/vnd.openxmlformats-officedocument.presentationml.presentation", filename=file.name)
            if parts[3] == "report" and job.get("report"):
                return self.response(job["report"], filename="核对报告.json")
            return self.response({"error": "文件尚未生成"}, 404)
        static = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css"}
        if path in static:
            file = ROOT / "web" / static[path]
            return self.response(file.read_bytes(), mime=(mimetypes.guess_type(file.name)[0] or "text/plain") + "; charset=utf-8")
        return self.response({"error": "未找到页面"}, 404)

    def do_POST(self):
        if not self.trusted_host() or self.headers.get("X-AutoPPT-Token") != TOKEN:
            return self.response({"error": "请求校验失败，请刷新页面"}, 403)
        origin = self.headers.get("Origin")
        if origin and urllib.parse.urlsplit(origin).netloc != self.headers.get("Host"):
            return self.response({"error": "拒绝跨站请求"}, 403)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_UPLOAD:
                raise ValueError("上传总大小必须小于400MB")
            body = self.rfile.read(length)
            path = urllib.parse.urlsplit(self.path).path
            if path == "/api/sample":
                sample = json.loads(body).get("name")
                if sample not in ("input", "input1"):
                    raise ValueError("示例目录无效")
                template, agenda, experts = discover(ROOT / sample)
                job = new_job()
                threading.Thread(target=analyze_job, args=(job, template, agenda, experts), daemon=True).start()
                return self.response({"id": job["id"]}, 202)
            if path == "/api/analyze":
                content_type = self.headers.get("Content-Type", "")
                if "multipart/form-data" not in content_type:
                    raise ValueError("请使用文件上传表单")
                message = BytesParser(policy=policy.default).parsebytes(("Content-Type: " + content_type + "\r\nMIME-Version: 1.0\r\n\r\n").encode() + body)
                job = new_job()
                files = {"template": [], "agenda": [], "experts": []}
                for index, part in enumerate(message.iter_parts()):
                    kind, name = part.get_param("name", header="content-disposition"), part.get_filename()
                    if kind not in files or not name:
                        continue
                    name = Path(name.replace("\\", "/")).name
                    ext = Path(name).suffix.lower()
                    if ext not in ((".pptx",) if kind != "experts" else (".pptx", ".docx", ".zip")):
                        raise ValueError(f"不支持的文件：{name}")
                    data = part.get_payload(decode=True)
                    if len(data) > MAX_FILE:
                        raise ValueError("单个文件超过200MB")
                    directory = job["directory"] / kind / str(index)
                    directory.mkdir(parents=True)
                    file = directory / name
                    file.write_bytes(data)
                    files[kind].append(file)
                if len(files["template"]) != 1 or len(files["agenda"]) != 1 or not files["experts"]:
                    raise ValueError("需要1份模板、1份日程和至少1份专家资料")
                threading.Thread(target=analyze_job, args=(job, files["template"][0], files["agenda"][0], files["experts"]), daemon=True).start()
                return self.response({"id": job["id"]}, 202)
            if path.startswith("/api/jobs/") and path.endswith("/generate"):
                identifier = path.split("/")[3]
                job = JOBS.get(identifier)
                if not job or "model" not in job:
                    raise ValueError("任务未准备好")
                with LOCK:
                    if job["status"] == "generating":
                        raise ValueError("该任务正在生成，请等待完成")
                    model = apply_edits(job["model"], json.loads(body))
                    # 先验证页面映射，错误直接返回给编辑界面。
                    page_plan(model)
                    job.pop("generation_error", None)
                    job.update(status="generating", message="正在填充模板和校验PPT")
                threading.Thread(target=generate_job, args=(job, model), daemon=True).start()
                return self.response({"id": identifier}, 202)
            return self.response({"error": "未知操作"}, 404)
        except (ValueError, KeyError, TypeError) as exc:
            return self.response({"error": str(exc)}, 400)
        except Exception as exc:
            traceback.print_exc()
            return self.response({"error": "处理失败：" + str(exc)}, 500)


def serve(port=8765, browser=True):
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError:
        # 重复双击启动文件时复用已经运行的本机工作台。
        import urllib.request
        existing = f'http://127.0.0.1:{port}'
        try:
            with urllib.request.urlopen(existing + '/api/config', timeout=2) as response:
                data = json.load(response)
            if data.get('roles') != ROLES:
                raise ValueError('端口被其他程序使用')
        except Exception:
            raise OSError(f'端口{port}已被占用，请使用--port指定其他端口') from None
        if browser:
            import webbrowser
            webbrowser.open(existing)
        print(f'工作台已经运行：{existing}', flush=True)
        return
    url = f"http://127.0.0.1:{server.server_port}"
    print(f"AutoPPT已启动：{url}\n文件仅在本机处理，按Ctrl+C退出。", flush=True)
    if browser:
        import webbrowser
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
