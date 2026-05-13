#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import urlencode


ROOT = Path(__file__).resolve().parents[1]
INDEX_PATH = ROOT / "index.html"
API_BASE = "https://bbs.auditdog.cn"
MAX_DAYS = 3
MAX_PAGES = 3
LOCAL_TZ = timezone(timedelta(hours=8))


@dataclass
class UpdateStats:
    total_records: int
    updated_records: int
    added_records: int
    oldest: str
    newest: str
    refreshed_at: str


def run_curl_json(path: str, params: dict[str, Any]) -> dict[str, Any]:
    url = f"{API_BASE}{path}?{urlencode(params)}"
    raw = subprocess.check_output(
        ["curl", "-fsSL", url],
        text=True,
    )
    return json.loads(raw)


def strip_html(value: Any) -> str:
    text = re.sub(r"<[^>]+>", "", str(value or ""))
    text = unescape(text)
    return text.replace("\xa0", " ").strip()


def parse_datetime(value: str) -> datetime | None:
    if not value:
      return None
    try:
        if value.endswith("Z"):
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def to_local_text(value: str) -> str:
    dt = parse_datetime(value)
    if dt is None:
        return value[:19].replace("T", " ").replace("Z", "")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    else:
        dt = dt.astimezone(LOCAL_TZ)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def extract_script_json(html: str, element_id: str) -> str:
    pattern = rf'(<script id="{re.escape(element_id)}" type="application/json">)(.*?)(</script>)'
    match = re.search(pattern, html, re.DOTALL)
    if not match:
        raise RuntimeError(f"Cannot find {element_id!r} in index.html")
    return match.group(2)


def replace_script_json(html: str, element_id: str, payload: str) -> str:
    pattern = rf'(<script id="{re.escape(element_id)}" type="application/json">)(.*?)(</script>)'
    return re.sub(
        pattern,
        lambda m: f"{m.group(1)}{payload}{m.group(3)}",
        html,
        count=1,
        flags=re.DOTALL,
    )


def replace_meta_line(html: str, text: str) -> str:
    pattern = r'(<div class="meta">)(.*?)(</div>)'
    return re.sub(
        pattern,
        lambda m: f"{m.group(1)}{text}{m.group(3)}",
        html,
        count=1,
        flags=re.DOTALL,
    )


def thread_id_from_link(link: str) -> str:
    match = re.search(r"thread-(\d+)-", link or "")
    return match.group(1) if match else ""


def convert_recent_item(item: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
    posts = item.get("posts") or []
    question_chunks: list[str] = []
    reply_chunks: list[str] = []

    first_question_author = ""
    first_question_time = ""
    last_reply_time = ""

    for post in posts:
        question_text = strip_html(post.get("question_text"))
        reply_text = strip_html(post.get("comment_text"))
        if question_text:
            question_chunks.append(question_text)
        if reply_text:
            reply_chunks.append(reply_text)

        if not first_question_author:
            first_question_author = strip_html(post.get("question_author"))
        if not first_question_time:
            first_question_time = strip_html(post.get("question_time"))
        if strip_html(post.get("comment_time")):
            last_reply_time = strip_html(post.get("comment_time"))

    latest_time = to_local_text(item.get("latest_time", "")) if item.get("latest_time") else last_reply_time
    reply_time = last_reply_time or latest_time
    tid = thread_id_from_link(item.get("link", ""))

    category = (existing or {}).get("category") or "最近更新"

    question = "\n\n---\n\n".join(question_chunks).strip()
    reply = "\n\n---\n\n".join(reply_chunks).strip()
    search_text = " ".join(
        part
        for part in [
            tid,
            item.get("title", ""),
            category,
            first_question_author,
            first_question_time,
            reply_time,
            question,
            reply,
            item.get("link", ""),
        ]
        if part
    ).lower()

    return {
        "tid": tid,
        "title": item.get("title", ""),
        "category": category,
        "questionAuthor": first_question_author,
        "questionTime": first_question_time,
        "replyTime": reply_time,
        "replySortTime": reply_time,
        "url": item.get("link", ""),
        "question": question,
        "reply": reply,
        "searchText": search_text,
    }


def refresh_recent_records(existing_records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    by_tid = {str(row.get("tid", "")): row for row in existing_records if row.get("tid")}
    updated = 0
    page = 1
    while page <= MAX_PAGES:
        data = run_curl_json("/api/public/recent", {"days": MAX_DAYS, "page": page})
        for item in data.get("results", []):
            tid = thread_id_from_link(item.get("link", ""))
            if not tid:
                continue
            existing = by_tid.get(tid)
            new_record = convert_recent_item(item, existing)
            if existing is None:
                by_tid[tid] = new_record
                updated += 1
                continue

            current_time = existing.get("replySortTime", "")
            incoming_time = new_record.get("replySortTime", "")
            if not current_time or incoming_time >= current_time:
                merged = {**existing, **new_record}
                merged["category"] = existing.get("category") or new_record["category"]
                by_tid[tid] = merged
                updated += 1
        if not data.get("hasMore"):
            break
        page += 1

    records = list(by_tid.values())
    records.sort(key=lambda row: row.get("replySortTime", ""), reverse=True)
    return records, updated


def rebuild_categories(existing_categories: list[str], records: list[dict[str, Any]]) -> list[str]:
    categories = list(existing_categories)
    seen = set(categories)
    for row in records:
        category = row.get("category", "")
        if category and category not in seen:
            categories.append(category)
            seen.add(category)
    return categories


def update_index() -> UpdateStats:
    html = INDEX_PATH.read_text(encoding="utf-8")
    records_json = extract_script_json(html, "recordsData")
    categories_json = extract_script_json(html, "categoriesData")
    records: list[dict[str, Any]] = json.loads(records_json)
    categories: list[str] = json.loads(categories_json)

    refreshed_records, changed_count = refresh_recent_records(records)
    refreshed_categories = rebuild_categories(categories, refreshed_records)

    if refreshed_records:
        oldest = min((row.get("replySortTime", "") for row in refreshed_records if row.get("replySortTime")), default="")
        newest = max((row.get("replySortTime", "") for row in refreshed_records if row.get("replySortTime")), default="")
    else:
        oldest = newest = ""

    updated_html = replace_script_json(html, "recordsData", json.dumps(refreshed_records, ensure_ascii=False))
    updated_html = replace_script_json(updated_html, "categoriesData", json.dumps(refreshed_categories, ensure_ascii=False))
    updated_html = replace_meta_line(
        updated_html,
        f"{len(refreshed_records)} 条主题｜2026-01-01 至 {newest[:10] if newest else '—'}｜CPA业务探讨版",
    )
    INDEX_PATH.write_text(updated_html, encoding="utf-8")

    return UpdateStats(
        total_records=len(refreshed_records),
        updated_records=changed_count,
        added_records=max(0, len(refreshed_records) - len(records)),
        oldest=oldest,
        newest=newest,
        refreshed_at=datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S"),
    )


def main() -> None:
    stats = update_index()
    print(
        json.dumps(
            {
                "total_records": stats.total_records,
                "updated_records": stats.updated_records,
                "added_records": stats.added_records,
                "oldest": stats.oldest,
                "newest": stats.newest,
                "refreshed_at": stats.refreshed_at,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
