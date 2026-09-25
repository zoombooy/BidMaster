"""命令行入口：
  python -m bidmaster analyze <招标文件> [--workdir work] [--force] [--no-llm]
  python -m bidmaster serve [--port 8000]
"""
from __future__ import annotations

import argparse
import sys


def _print_report(report_dict: dict) -> None:
    stats = report_dict.get("stats", {})
    print("=" * 62)
    print(f"文档: {report_dict.get('file_name')}  (doc_id={report_dict.get('doc_id')})")
    print(f"统计: 字段 {stats.get('fields_found')}/{stats.get('fields_total')}"
          f" | 评分项 {stats.get('score_items')}"
          f" | 要求条目 {stats.get('requirements')}"
          f" | ★条款 {stats.get('star_clauses')}"
          f" | LLM {stats.get('llm')}")
    print("-" * 62)

    print("\n【关键字段】")
    for key, f in report_dict.get("fields", {}).items():
        mark = "✓" if f.get("status") == "found" else "✗"
        label = f.get("field_label") or key
        value = f.get("value_normalized") if f.get("status") == "found" else "未发现"
        conf = f.get("confidence") or 0
        flag = " ⚠低置信" if f.get("status") == "found" and conf < 0.7 else ""
        print(f"  {mark} {label:<12} {str(value)[:44]:<46} ({conf:.2f}){flag}")

    conflicts = report_dict.get("conflicts", [])
    if conflicts:
        print("\n【字段冲突（请人工确认）】")
        for c in conflicts:
            print(f"  ! {c['field_key']}: 取 {c['chosen']} / 其他来源 "
                  f"{[str(a['value_normalized']) for a in c['alternatives']]}")

    print("\n【评分标准】")
    for chk in report_dict.get("sum_checks", []):
        note = f"  ⚠ {chk['note']}" if not chk.get("ok") else ""
        print(f"  {chk['category']:<11} 项数 {chk['item_count']:<3}"
              f" 合计 {chk['computed_total']:<6}"
              f" 声明 {chk.get('declared_total')}{note}")
    for s in report_dict.get("scores", []):
        print(f"  {s['score_id']:<7} {s['name'][:22]:<24} {s['max_score']:>5}分"
              f"  置信 {s['confidence']:.2f}")

    print("\n【要求条目】")
    for r in report_dict.get("requirements", []):
        parsed = r.get("parsed") or {}
        brief = ", ".join(f"{k}={v}" for k, v in list(parsed.items())[:3])
        print(f"  {r.get('req_id')} [{r.get('type')}] {r.get('subject')}: {brief[:70]}")

    stars = report_dict.get("star_clauses", [])
    if stars:
        print(f"\n【★实质性条款】共 {len(stars)} 条")
        for st in stars[:5]:
            print(f"  {st.get('clause_id')} p{st.get('page_no')}: {st.get('text')[:60]}")

    issues = report_dict.get("issues", [])
    if issues:
        print("\n【问题清单】")
        for i in issues:
            print(f"  ! {i}")

    led = report_dict.get("ledger", {})
    print(f"\n【账本】对象 {led.get('total')} | 完成 {led.get('done')}"
          f" | 排除 {led.get('excluded')} | 转人工 {led.get('failed_review')}"
          f" | 完成门 {'通过' if led.get('complete_gate_passed') else '未通过 ⚠'}")
    print("=" * 62)


def main(argv=None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="bidmaster", description="招标文件解析 Agent（一期）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_an = sub.add_parser("analyze", help="解析招标文件并输出结构化报告（支持多个文件合并，如 公告+正文）")
    p_an.add_argument("file", nargs="+", help="招标文件路径（.docx/.pdf）")
    p_an.add_argument("--workdir", default="work")
    p_an.add_argument("--force", action="store_true", help="忽略缓存强制重跑（级联失效下游）")
    p_an.add_argument("--no-llm", action="store_true", help="禁用 LLM 兜底（纯规则模式）")
    p_an.add_argument("--json", action="store_true", help="输出完整 JSON 报告")

    p_sv = sub.add_parser("serve", help="启动 API 服务")
    p_sv.add_argument("--port", type=int, default=8000)
    p_sv.add_argument("--host", default="127.0.0.1")

    args = parser.parse_args(argv)
    if args.cmd == "analyze":
        from bidmaster.orchestration.pipeline import Pipeline
        report = Pipeline(work_root=args.workdir).run(
            args.file, force=args.force, use_llm=not args.no_llm)
        if args.json:
            import json
            print(json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2))
        else:
            _print_report(report.model_dump(mode="json"))
        return 0
    if args.cmd == "serve":
        import uvicorn
        uvicorn.run("bidmaster.api.main:app", host=args.host, port=args.port)
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
