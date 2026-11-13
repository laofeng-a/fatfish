#!/usr/bin/env python3
"""Offline Chinese recall baseline for the current Fatfish lexical tools.

Loads a synthetic corpus into a temporary runtime.sqlite and notes directory,
then scores history_find, chat_recall, memory_find and diary search_notes.
Does not change those implementations.

Run from the repository root:

    python3 server-setup/tests/recall_baseline.py
    py -3 server-setup\\tests\\recall_baseline.py
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import platform
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from memory_notes import UTC8  # noqa: E402
from runtime_policy import Runtime  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "recall_fixtures"
TOOLS = ("history_find", "chat_recall", "memory_find", "search_notes")
KINDS = ("exact", "paraphrase", "distractor")
LIMIT = 8
DEFAULT_REPS = 9


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def percentile(values, p):
    """Linear interpolation between sorted samples. p is 0..100."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * (p / 100)
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return ordered[low]
    weight = rank - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def diary_path(day_offset, today):
    if type(day_offset) is not int or day_offset > 0:
        raise ValueError("日记 day_offset 必须是不大于 0 的整数")
    day = today + dt.timedelta(days=day_offset)
    return day.strftime("%Y%m/%Y%m%d.md")


def build_runtime(directory: Path) -> Runtime:
    catalog = directory / "catalog.json"
    catalog.write_text("[]", encoding="utf-8")
    settings = {
        "owner_id": "9001",
        "state_dir": str(directory),
        "catalog_file": str(catalog),
        "bili_cookie_file": str(directory / "missing-cookies"),
        "platform_read_policy": "model",
        "publication_policy": "model",
    }
    path = directory / "settings.json"
    path.write_text(json.dumps(settings), encoding="utf-8")
    runtime = Runtime(path)
    # Corpus owner is fixed in the fixture; keep the settings file and the
    # runtime in agreement if a caller edits the JSON.
    return runtime


def seed(runtime: Runtime, corpus: dict):
    owner = str(corpus["owner_id"])
    chats = {"owner": owner, "other": str(corpus["other_chat_id"])}
    if runtime.owner != owner:
        raise RuntimeError("语料 owner_id 与运行时不一致")
    context = {
        "channel": "telegram",
        "chat_id": owner,
        "message_id": "1",
        "session_key": "telegram:" + owner,
    }
    documents = {"events": {}, "memories": {}, "notes": {}}
    event_ids = {}
    memory_ids = {}
    for item in corpus["events"]:
        chat = chats[item["chat"]]
        event_id = runtime.event(chat, item["kind"], item["content"], item["msg"])
        if not event_id:
            raise RuntimeError("事件没有写入: " + item["key"])
        event_ids[item["key"]] = event_id
        documents["events"][item["key"]] = item["content"]
    for item in corpus["memories"]:
        saved = runtime.call(
            "memory_save",
            {
                "category": item["category"],
                "content": item["content"],
                "source_id": event_ids[item["source"]],
                "quote": item["quote"],
                "importance": item.get("importance", 0.5),
                "expires_days": item.get("expires_days", 30),
                "_context": context,
            },
        )
        documents["memories"][item["key"]] = item["content"]
        memory_ids[item["key"]] = saved["id"]
    today = dt.datetime.now(UTC8).date()
    note_paths = {}
    for item in corpus["notes"]:
        path = item.get("path") or diary_path(item.get("day_offset"), today)
        current = runtime.call("memory_note_read", {"path": path, "_context": context})
        runtime.call(
            "memory_note_write",
            {
                "path": path,
                "content": item["content"],
                "expected_revision": current["revision"],
                "_context": context,
            },
        )
        documents["notes"][item["key"]] = item["content"]
        note_paths[item["key"]] = path
    path_to_key = {path: key for key, path in note_paths.items()}
    return {
        "context": context,
        "documents": documents,
        "event_keys": {value: key for key, value in event_ids.items()},
        "memory_keys": {value: key for key, value in memory_ids.items()},
        "path_to_key": path_to_key,
        "note_paths": note_paths,
    }


def validate_cases(cases, documents):
    if not 20 <= len(cases) <= 40:
        raise ValueError("用例数量需要在 20 到 40 之间，当前是 %d" % len(cases))
    seen = set()
    covered = {(tool, kind) for tool in TOOLS for kind in KINDS}
    for case in cases:
        if case["id"] in seen:
            raise ValueError("重复用例 " + case["id"])
        seen.add(case["id"])
        tool, kind = case["tool"], case["kind"]
        if tool not in TOOLS or kind not in KINDS:
            raise ValueError("未知工具或类型: " + case["id"])
        covered.discard((tool, kind))
        expect = case.get("expect") or []
        if kind in ("exact", "paraphrase") and len(expect) != 1:
            raise ValueError("精确和改写用例需要恰好一个期望文档: " + case["id"])
        if kind == "distractor" and not case.get("expect_empty") and not case.get("forbid_top1"):
            raise ValueError("干扰用例需要 expect_empty 或 forbid_top1: " + case["id"])
        store = {"history_find": "events", "chat_recall": "events", "memory_find": "memories", "search_notes": "notes"}[tool]
        known = documents[store]
        for key in list(expect) + list(case.get("forbid_top1") or []):
            if key not in known:
                raise ValueError("%s 引用了不存在的文档 %s" % (case["id"], key))
        if kind in ("exact", "paraphrase"):
            text = known[expect[0]]
            contained = case["query"] in text
            if kind == "exact" and not contained:
                raise ValueError("精确查询不是目标原文的子串: " + case["id"])
            if kind == "paraphrase" and contained:
                raise ValueError("改写查询其实是目标原文的子串: " + case["id"])
    if covered:
        missing = ", ".join("%s/%s" % pair for pair in sorted(covered))
        raise ValueError("缺少这类用例: " + missing)


def ranked_keys(tool, result, seeded):
    if tool == "history_find":
        return [seeded["event_keys"].get(row["id"], "event:%s" % row["id"]) for row in result["records"]]
    if tool == "chat_recall":
        return [seeded["event_keys"].get(item, "event:%s" % item) for item in result["matched_ids"]]
    if tool == "memory_find":
        return [seeded["memory_keys"].get(row["id"], "memory:%s" % row["id"]) for row in result["memories"]]
    return [seeded["path_to_key"].get(row["path"], row["path"]) for row in result]


def call_tool(runtime, tool, query, context):
    if tool == "history_find":
        return runtime.call("history_find", {"query": query, "limit": LIMIT, "_context": context})
    if tool == "chat_recall":
        return runtime.call(
            "chat_recall",
            {"query": query, "limit": LIMIT, "around": 0, "_context": context},
        )
    if tool == "memory_find":
        return runtime.call("memory_find", {"query": query, "limit": LIMIT, "_context": context})
    if tool == "search_notes":
        return runtime.notes.search(query)
    raise ValueError(tool)


def evaluate_case(case, ranked):
    expect = case.get("expect") or []
    top1 = ranked[0] if ranked else None
    false_positive = False
    if case.get("expect_empty") and ranked:
        false_positive = True
    if top1 in set(case.get("forbid_top1") or []):
        false_positive = True
    return {
        "id": case["id"],
        "tool": case["tool"],
        "kind": case["kind"],
        "query": case["query"],
        "expect": expect,
        "expect_empty": bool(case.get("expect_empty")),
        "forbid_top1": list(case.get("forbid_top1") or []),
        "top": ranked[:LIMIT],
        "hit_at_1": bool(expect) and bool(ranked) and ranked[0] in expect,
        "hit_at_5": bool(expect) and any(item in expect for item in ranked[:5]),
        "false_positive": false_positive,
        "zero_results": not ranked,
        "note": case.get("note", ""),
    }


def summarize(results, latencies):
    summary = {}
    for tool in TOOLS:
        rows = [row for row in results if row["tool"] == tool]
        graded = [row for row in rows if row["kind"] in ("exact", "paraphrase")]
        paraphrases = [row for row in rows if row["kind"] == "paraphrase"]
        exact = [row for row in rows if row["kind"] == "exact"]
        distractors = [row for row in rows if row["kind"] == "distractor"]
        samples = latencies[tool]

        def rate(group, field):
            if not group:
                return None
            return sum(1 for row in group if row[field]) / len(group)

        summary[tool] = {
            "cases": len(rows),
            "graded_cases": len(graded),
            "exact_cases": len(exact),
            "paraphrase_cases": len(paraphrases),
            "distractor_cases": len(distractors),
            "hit_at_1": rate(graded, "hit_at_1"),
            "hit_at_5": rate(graded, "hit_at_5"),
            "exact_hit_at_1": rate(exact, "hit_at_1"),
            "exact_hit_at_5": rate(exact, "hit_at_5"),
            "paraphrase_hit_at_1": rate(paraphrases, "hit_at_1"),
            "paraphrase_hit_at_5": rate(paraphrases, "hit_at_5"),
            "distractor_false_positive_rate": rate(distractors, "false_positive"),
            "distractor_false_positives": sum(1 for row in distractors if row["false_positive"]),
            "zero_result_count": sum(1 for row in rows if row["zero_results"]),
            "zero_result_on_expected_hit": sum(1 for row in graded if row["zero_results"]),
            "latency_ms": {
                "p50": percentile(samples, 50),
                "p95": percentile(samples, 95),
                "samples": len(samples),
            },
        }
    return summary


def run(reps=DEFAULT_REPS, fixtures=FIXTURES):
    if reps < 1:
        raise ValueError("reps 至少为 1")
    corpus = load_json(fixtures / "corpus.json")
    cases = load_json(fixtures / "cases.json")
    with tempfile.TemporaryDirectory(prefix="recall-baseline-") as temporary:
        runtime = build_runtime(Path(temporary))
        seeded = seed(runtime, corpus)
        validate_cases(cases, seeded["documents"])
        for tool in TOOLS:
            call_tool(runtime, tool, cases[0]["query"], seeded["context"])
        results = []
        latencies = {tool: [] for tool in TOOLS}
        for case in cases:
            rankings = []
            for _ in range(reps):
                started = time.perf_counter()
                payload = call_tool(runtime, case["tool"], case["query"], seeded["context"])
                elapsed_ms = (time.perf_counter() - started) * 1000
                latencies[case["tool"]].append(elapsed_ms)
                rankings.append(ranked_keys(case["tool"], payload, seeded))
            if any(item != rankings[0] for item in rankings[1:]):
                raise RuntimeError("同一用例的排序不稳定: " + case["id"])
            row = evaluate_case(case, rankings[0])
            row["latency_ms_p50"] = percentile(
                latencies[case["tool"]][-reps:],
                50,
            )
            results.append(row)
    report = {
        "schema_version": 1,
        "description": "当前词法实现的中文召回基线。没有改查询、索引或排序。",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "limit": LIMIT,
        "reps": reps,
        "definitions": {
            "hit_at_1": "精确与改写用例中，期望文档排在第 1 的比例。",
            "hit_at_5": "精确与改写用例中，期望文档出现在前 5 的比例。",
            "paraphrase_hit_at_5": "只统计改写用例的 hit@5，作为改写命中率。",
            "distractor_false_positive_rate": "干扰用例中，结果非空但期望为空，或禁止项排在第 1 的比例。",
            "zero_result_count": "该工具全部用例里返回空列表的个数。",
            "latency_ms": "每个用例重复 reps 次，按工具汇总。百分位用线性插值。正式计时前每个工具有一次预热，不计入。",
            "history_rank": "history_find 用 records 的返回顺序（当前是 id 从新到旧）。",
            "chat_rank": "chat_recall 用 matched_ids 的顺序，around=0，不把前后文当成命中。",
            "memory_rank": "memory_find 只看 memories，不把同一次返回的 notes 算进这个工具。",
            "notes_rank": "search_notes 使用 MemoryNotes.search 的返回顺序。",
        },
        "tools": summarize(results, latencies),
        "cases": results,
    }
    return report


def round_report(report):
    def walk(value):
        if isinstance(value, float):
            return round(value, 4) if value <= 1 else round(value, 3)
        if isinstance(value, dict):
            return {key: walk(item) for key, item in value.items()}
        if isinstance(value, list):
            return [walk(item) for item in value]
        return value

    return walk(report)


def comparable(report):
    """Drop fields that change between machines or runs."""
    data = json.loads(json.dumps(round_report(report)))
    for field in ("generated_at", "python", "platform", "reps"):
        data.pop(field, None)
    for tool in data.get("tools", {}).values():
        tool.pop("latency_ms", None)
    for case in data.get("cases", []):
        case.pop("latency_ms_p50", None)
    return data


def format_rate(value):
    if value is None:
        return "  n/a"
    return "%5.1f%%" % (value * 100)


def format_ms(value):
    if value is None:
        return "  n/a"
    return "%7.2f" % value


def render_table(report):
    lines = [
        "中文召回基线（当前实现，未改检索）",
        "limit=%d  reps=%d  %s" % (report["limit"], report["reps"], report["generated_at"]),
        "",
        "%-14s %5s %7s %7s %8s %8s %6s %6s %8s %8s"
        % ("工具", "用例", "hit@1", "hit@5", "改写@1", "改写@5", "误报", "零结果", "p50毫秒", "p95毫秒"),
    ]
    for tool in TOOLS:
        row = report["tools"][tool]
        latency = row["latency_ms"]
        lines.append(
            "%-14s %5d %7s %7s %8s %8s %5.1f%% %6d %8s %8s"
            % (
                tool,
                row["cases"],
                format_rate(row["hit_at_1"]),
                format_rate(row["hit_at_5"]),
                format_rate(row["paraphrase_hit_at_1"]),
                format_rate(row["paraphrase_hit_at_5"]),
                (row["distractor_false_positive_rate"] or 0) * 100,
                row["zero_result_count"],
                format_ms(latency["p50"]),
                format_ms(latency["p95"]),
            )
        )
    misses = [row for row in report["cases"] if row["kind"] in ("exact", "paraphrase") and not row["hit_at_5"]]
    false_hits = [row for row in report["cases"] if row["kind"] == "distractor" and row["false_positive"]]
    lines.append("")
    lines.append("改写或精确查询未进前 5（%d）:" % len(misses))
    if not misses:
        lines.append("  无")
    for row in misses:
        lines.append("  %s  %s  top=%s" % (row["id"], row["query"], "、".join(row["top"][:3]) or "空"))
    lines.append("干扰项误报（%d）:" % len(false_hits))
    if not false_hits:
        lines.append("  无")
    for row in false_hits:
        lines.append("  %s  %s  top=%s" % (row["id"], row["query"], "、".join(row["top"][:3]) or "空"))
    return "\n".join(lines)


def write_report(report, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(round_report(report), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description="运行中文召回基线并写出 JSON")
    parser.add_argument("--out", type=Path, default=FIXTURES / "baseline_results.json")
    parser.add_argument("--reps", type=int, default=DEFAULT_REPS)
    parser.add_argument("--check", action="store_true", help="只比较命中和误报，不比较耗时")
    args = parser.parse_args(argv)
    report = round_report(run(reps=args.reps))
    print(render_table(report))
    if args.check:
        previous = round_report(load_json(args.out))
        if comparable(previous) != comparable(report):
            print("\n与 %s 的命中结果不一致。" % args.out, file=sys.stderr)
            return 1
        print("\n命中结果与 %s 一致（耗时不参与比较）。" % args.out)
        return 0
    write_report(report, args.out)
    print("\n已写入 %s" % args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
