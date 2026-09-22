"""网页入口与批量生成命令行入口。"""

import argparse
import json
import sys
from pathlib import Path


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="本地会议PPT生成工具，零大模型调用")
    parser.add_argument("--input", type=Path, help="包含日程、模板和专家资料的目录")
    parser.add_argument("--template", type=Path, help="显式指定模板PPTX")
    parser.add_argument("--agenda", type=Path, help="显式指定日程PPTX")
    parser.add_argument("--experts", type=Path, nargs="+", help="专家DOCX、WPS、PPTX或ZIP文件")
    parser.add_argument("--output", type=Path, default=Path("output/会议串场.pptx"))
    parser.add_argument("--analyze-only", action="store_true", help="只输出识别结果")
    parser.add_argument("--draft", action="store_true", help="缺少简介时生成标有待补充内容的版本")
    parser.add_argument("--include-topics", action="store_true", help="每个讨论环节沿用模板中的话题页")
    parser.add_argument("--no-repeat-chairs", action="store_true")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    if args.input or args.template:
        from autoppt.analyze import analyze, discover
        from autoppt.generate import generate
        if args.input:
            template, agenda, experts = discover(args.input)
        else:
            if not args.agenda or not args.experts:
                parser.error("显式指定模板时，还需要--agenda和--experts")
            template, agenda, experts = args.template, args.agenda, args.experts
        work = Path("work/cli")
        work.mkdir(parents=True, exist_ok=True)
        model = analyze(template, agenda, experts, work)
        model["options"].update(draft=args.draft, include_topics=args.include_topics, repeat_chairs=not args.no_repeat_chairs)
        (work / "analysis.json").write_text(json.dumps(model, ensure_ascii=False, indent=2), encoding="utf-8")
        if args.analyze_only:
            print(json.dumps(model, ensure_ascii=False, indent=2))
        else:
            report = generate(model, args.output)
            print(f"已生成：{args.output.resolve()}\n页数：{len(report['pages'])}\n性能：{report['metrics']}")
        return
    from autoppt.server import serve
    serve(args.port, not args.no_browser)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"处理失败：{exc}", file=sys.stderr)
        sys.exit(1)
