"""선수별 뉴스 반응 수집기.

1) 구글 뉴스 RSS(한국어)로 선수별 최근 7일 기사 제목을 가져온다 (키 필요 없음)
   네이버 검색 API 키가 있으면 네이버 뉴스도 함께 가져온다
2) players.json 규칙(must_any / exclude)으로 1차 거른다
3) Claude로 관련성·감성·주제·키워드를 붙인다
4) 최근 N일을 집계해 reactions.json 을 만든다 (도감 페이지가 읽는 파일)

환경변수:
  ANTHROPIC_API_KEY                     Claude API 키 (필수)
  NAVER_CLIENT_ID, NAVER_CLIENT_SECRET  네이버 검색 API 키 (선택)
  CLAUDE_MODEL                          기본 claude-opus-5-5 (싸게 돌리려면 claude-haiku-5-5)

실행:
  python collect.py               수집 + 분류 + 집계
  python collect.py --no-llm      수집만 (분류·집계 건너뜀)
  python collect.py --aggregate   이미 쌓인 데이터로 집계만
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB_PATH = HERE / "reactions.db"
PLAYERS_PATH = HERE / "players.json"
OUT_PATH = HERE / "reactions.json"

KST = timezone(timedelta(hours=9))
WINDOW_DAYS = 7
BATCH_SIZE = 20
TOPICS = ["경기력", "이적", "부상", "대표팀", "기록", "논란", "인터뷰", "기타"]
MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5-5")


# ---------- 저장소 ----------
def open_db() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS mentions(
          id TEXT PRIMARY KEY,          -- 원문 URL 해시
          player_no INTEGER NOT NULL,
          source TEXT NOT NULL,
          url TEXT NOT NULL,
          title TEXT NOT NULL,
          snippet TEXT,
          published_at TEXT NOT NULL,   -- ISO, KST
          collected_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS labels(
          mention_id TEXT PRIMARY KEY REFERENCES mentions(id),
          relevant INTEGER NOT NULL,
          sentiment TEXT NOT NULL,
          topics TEXT NOT NULL,         -- JSON 배열
          keywords TEXT NOT NULL,       -- JSON 배열
          model TEXT NOT NULL,
          labeled_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_mentions_player ON mentions(player_no, published_at);
        """
    )
    return db


def now_kst() -> datetime:
    return datetime.now(KST)


# ---------- 1. 수집 ----------
TAG_RE = re.compile(r"<[^>]+>")


def clean(text: str) -> str:
    return html.unescape(TAG_RE.sub("", text or "")).strip()


UA = "Mozilla/5.0 (compatible; PlayerDexReactions/0.1)"


def google_news(query: str) -> list[dict]:
    """구글 뉴스 RSS 검색. 공식 API가 아니라 형식이 바뀔 수 있음."""
    q = f"{query} when:{WINDOW_DAYS}d"
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": q, "hl": "ko", "gl": "KR", "ceid": "KR:ko"}
    )
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as res:
        root = ET.fromstring(res.read())
    out = []
    for it in root.iter("item"):
        title = clean(it.findtext("title"))
        source = clean(it.findtext("source"))
        if source and title.endswith(" - " + source):  # "제목 - 언론사" 꼴에서 언론사 떼기
            title = title[: -len(" - " + source)]
        out.append({"title": title, "snippet": source, "url": it.findtext("link") or "",
                    "pubDate": it.findtext("pubDate") or ""})
    return out


def naver_news(query: str) -> list[dict]:
    """네이버 검색 API. 키가 없으면 빈 목록."""
    cid, secret = os.environ.get("NAVER_CLIENT_ID"), os.environ.get("NAVER_CLIENT_SECRET")
    if not cid or not secret:
        return []
    url = "https://openapi.naver.com/v1/search/news.json?" + urllib.parse.urlencode(
        {"query": query, "display": 100, "sort": "date"}
    )
    req = urllib.request.Request(url, headers={"X-Naver-Client-Id": cid, "X-Naver-Client-Secret": secret})
    with urllib.request.urlopen(req, timeout=20) as res:
        items = json.load(res).get("items", [])
    return [{"title": clean(i.get("title")), "snippet": clean(i.get("description")),
             "url": i.get("originallink") or i.get("link"), "pubDate": i.get("pubDate", "")} for i in items]


SOURCES = {"google_news": google_news, "naver_news": naver_news}


def passes_rules(player: dict, title: str, text: str, source: str) -> bool:
    if any(word in text for word in player.get("exclude", [])):
        return False
    # 구글 뉴스는 요약이 없고 제목만 있어서 must_any를 걸면 너무 많이 버려짐 → 관련성은 Claude가 판단
    if source == "google_news":
        return True
    must = player.get("must_any", [])
    return not must or any(word in text for word in must)


def collect(db: sqlite3.Connection, players: list[dict]) -> int:
    added = 0
    stamp = now_kst().isoformat()
    cutoff = now_kst() - timedelta(days=WINDOW_DAYS)
    for p in players:
        name_keys = {p["name"].split()[-1], p["query"].split()[0]}
        for source, fetch in SOURCES.items():
            try:
                items = fetch(p["query"])
            except Exception as e:  # 한 선수·한 출처가 실패해도 나머지는 계속
                print(f"[수집 실패] {p['name']} / {source}: {e}")
                continue
            kept = 0
            for it in items:
                title, snippet = it["title"], it["snippet"]
                if not it["url"] or not any(k in title + snippet for k in name_keys):
                    continue
                if not passes_rules(p, title, title + " " + snippet, source):
                    continue
                try:
                    published_dt = parsedate_to_datetime(it["pubDate"]).astimezone(KST)
                except (TypeError, ValueError):
                    continue
                if published_dt < cutoff:
                    continue
                # 같은 기사가 두 출처에 다 나오면 제목으로 한 번만 저장
                title_key = re.sub(r"\W+", "", title)
                mid = hashlib.sha1(f"{p['no']}|{title_key}".encode()).hexdigest()[:16]
                cur = db.execute(
                    "INSERT OR IGNORE INTO mentions VALUES(?,?,?,?,?,?,?,?)",
                    (mid, p["no"], source, it["url"], title, snippet, published_dt.isoformat(), stamp),
                )
                kept += cur.rowcount
            added += kept
            if items:
                print(f"[수집] {p['name']} / {source}: 받은 기사 {len(items)}건, 새로 저장 {kept}건")
            time.sleep(1)
    db.commit()
    return added


# ---------- 2. 분류 ----------
SYSTEM = f"""너는 한국 축구 기사 헤드라인을 선수 한 명 기준으로 분류한다.
각 항목마다:
- relevant: 정말 이 축구 선수에 대한 기사인가. 동명이인(배우, 정치인 등)이거나 이름만 스쳐 지나가면 false.
- sentiment: 이 선수에게 기사가 주는 인상. pos / neu / neg.
  선수 기준으로 판단한다. 팀이 졌어도 선수가 골을 넣었으면 pos. 이적설·일정 안내처럼 평가가 없으면 neu.
  애매하면 neu.
- topics: {", ".join(TOPICS)} 중 0~2개.
- keywords: 내용을 나타내는 짧은 한국어 명사 1~3개. 선수 이름과 소속 클럽 이름은 빼라.
입력의 모든 id에 대해 하나씩 답한다."""

SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "relevant": {"type": "boolean"},
                    "sentiment": {"type": "string", "enum": ["pos", "neu", "neg"]},
                    "topics": {"type": "array", "items": {"type": "string", "enum": TOPICS}},
                    "keywords": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["id", "relevant", "sentiment", "topics", "keywords"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}


def classify(db: sqlite3.Connection, players: list[dict]) -> int:
    import anthropic

    client = anthropic.Anthropic()
    by_no = {p["no"]: p for p in players}
    rows = db.execute(
        """SELECT m.id, m.player_no, m.title, m.snippet FROM mentions m
           LEFT JOIN labels l ON l.mention_id = m.id
           WHERE l.mention_id IS NULL ORDER BY m.player_no, m.published_at DESC"""
    ).fetchall()
    groups: dict[int, list] = defaultdict(list)
    for r in rows:
        groups[r[1]].append(r)

    labeled = 0
    for no, items in groups.items():
        p = by_no.get(no)
        if not p:
            continue
        for i in range(0, len(items), BATCH_SIZE):
            batch = items[i : i + BATCH_SIZE]
            payload = "\n".join(json.dumps({"id": r[0], "title": r[2], "summary": r[3]}, ensure_ascii=False) for r in batch)
            prompt = f"선수: {p['name']} (소속 {p['club']})\n기사 목록(JSON Lines):\n{payload}"
            result = call_claude(client, prompt)
            if result is None:
                continue
            valid_ids = {r[0] for r in batch}
            stamp = now_kst().isoformat()
            for it in result.get("items", []):
                if it["id"] not in valid_ids:
                    continue
                db.execute(
                    "INSERT OR REPLACE INTO labels VALUES(?,?,?,?,?,?,?)",
                    (it["id"], int(it["relevant"]), it["sentiment"],
                     json.dumps(it["topics"], ensure_ascii=False),
                     json.dumps(it["keywords"][:3], ensure_ascii=False), MODEL, stamp),
                )
                labeled += 1
            db.commit()
            print(f"[분류] {p['name']}: {len(batch)}건")
    return labeled


def call_claude(client, prompt: str) -> dict | None:
    import anthropic

    for attempt in range(3):
        try:
            res = client.messages.create(
                model=MODEL,
                max_tokens=16000,
                system=SYSTEM,
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
                messages=[{"role": "user", "content": prompt}],
            )
        except anthropic.RateLimitError as e:
            wait = int(e.response.headers.get("retry-after", "30"))
            print(f"[대기] 요청 한도, {wait}초 뒤 다시 시도")
            time.sleep(wait)
            continue
        except anthropic.APIConnectionError as e:
            print(f"[재시도] 연결 오류: {e}")
            time.sleep(5)
            continue
        except anthropic.APIStatusError as e:
            if e.status_code >= 500:
                time.sleep(5)
                continue
            print(f"[분류 실패] {e.status_code}: {e.message}")
            return None
        if res.stop_reason in ("refusal", "max_tokens"):
            print(f"[분류 건너뜀] stop_reason={res.stop_reason} (다음 실행 때 다시 시도)")
            return None
        text = next((b.text for b in res.content if b.type == "text"), "")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            print("[분류 실패] JSON 파싱 오류")
            return None
    return None


# ---------- 3. 집계 ----------
def aggregate(db: sqlite3.Connection, players: list[dict]) -> dict:
    today = now_kst().date()
    days = [(today - timedelta(days=d)).isoformat() for d in range(WINDOW_DAYS - 1, -1, -1)]
    since = days[0]
    out = {"generated_at": now_kst().isoformat(timespec="minutes"), "window_days": WINDOW_DAYS,
           "source": "구글 뉴스" + (" · 네이버 뉴스" if os.environ.get("NAVER_CLIENT_ID") else ""),
           "players": {}}

    for p in players:
        rows = db.execute(
            """SELECT m.title, m.url, m.published_at, l.sentiment, l.topics, l.keywords
               FROM mentions m JOIN labels l ON l.mention_id = m.id
               WHERE m.player_no = ? AND l.relevant = 1 AND substr(m.published_at,1,10) >= ?
               ORDER BY m.published_at DESC""",
            (p["no"], since),
        ).fetchall()
        per_day = Counter(r[2][:10] for r in rows)
        senti = Counter(r[3] for r in rows)
        topics = Counter(t for r in rows for t in json.loads(r[4]))
        keywords = Counter(k for r in rows for k in json.loads(r[5]))
        out["players"][str(p["no"])] = {
            "n": len(rows),
            "sentiment": {k: senti.get(k, 0) for k in ("pos", "neu", "neg")},
            "daily": [{"date": d, "n": per_day.get(d, 0)} for d in days],
            "topics": [t for t, _ in topics.most_common(3)],
            "keywords": [k for k, _ in keywords.most_common(5)],
            "headlines": [{"title": r[0], "url": r[1], "date": r[2][:10], "sentiment": r[3]} for r in rows[:3]],
        }
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[집계] {OUT_PATH.name} 저장 ({len(players)}명)")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-llm", action="store_true", help="수집만 하고 분류·집계는 건너뜀")
    ap.add_argument("--aggregate", action="store_true", help="집계만 다시 실행")
    args = ap.parse_args()

    players = json.loads(PLAYERS_PATH.read_text(encoding="utf-8"))
    db = open_db()
    if args.aggregate:
        aggregate(db, players)
        return
    added = collect(db, players)
    print(f"새 기사 {added}건")
    if args.no_llm:
        return
    classify(db, players)
    aggregate(db, players)


if __name__ == "__main__":
    main()
