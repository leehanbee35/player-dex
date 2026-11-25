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

ROSTER_PATH = HERE.parent / "data" / "players.json"
# 자동 수집 대상: 한국 국적 선수 전원 + 아래 팀의 등번호 1~35번 선수
BIG_CLUBS = {
    "레알 마드리드", "바르셀로나", "아틀레티코 마드리드", "맨체스터 시티", "아스널", "리버풀", "첼시",
    "맨체스터 유나이티드", "토트넘 홋스퍼", "뉴캐슬 유나이티드", "바이에른 뮌헨", "도르트문트",
    "바이어 레버쿠젠", "파리 생제르맹", "인테르", "AC 밀란", "유벤투스", "나폴리",
}

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


# 욕설·비하 표현이 든 유튜브 댓글은 저장하지 않음 (사이트 댓글 금지어와 같은 목록)
BANNED = ["씨발", "시발", "씨팔", "ㅅㅂ", "ㅆㅂ", "병신", "ㅂㅅ", "ㅄ", "개새끼", "새끼", "좆", "지랄", "니애미", "느금",
          "애미", "애비", "엠창", "창녀", "짱깨", "쪽바리", "깜둥이", "흑돼지", "한남충", "김치녀", "꺼져", "닥쳐", "fuck", "shit"]


def is_clean(text: str) -> bool:
    flat = re.sub(r"[\s.\-_~!*]+", "", text).lower()
    return not any(w in flat for w in BANNED)


def yt_get(path: str, params: dict) -> dict:
    params = {**params, "key": os.environ.get("YOUTUBE_API_KEY", "").strip()}
    url = f"https://www.googleapis.com/youtube/v3/{path}?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=20) as res:
        return json.load(res)


def collect_youtube(db: sqlite3.Connection, players: list[dict], limit: int = 40) -> int:
    """유튜브 커뮤니티 반응: 하루 한 번, 화제 선수 영상 3개의 인기 댓글 10개씩.
    YOUTUBE_API_KEY가 없으면 건너뜀. 무료 할당량(하루 10,000)에 맞춰 선수 수를 제한함 (검색 1번 = 100)."""
    if not os.environ.get("YOUTUBE_API_KEY", "").strip():
        return 0
    stamp = now_kst().isoformat()
    since = (datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    # 최근 7일 기사가 많은 선수부터 (한국 선수는 항상 포함)
    counts = dict(db.execute(
        "SELECT player_no, COUNT(*) FROM mentions WHERE source != 'youtube' AND published_at >= ? GROUP BY player_no",
        ((now_kst() - timedelta(days=WINDOW_DAYS)).isoformat(),)).fetchall())
    korean = [p for p in players if p.get("korean")]
    ranked = sorted((p for p in players if not p.get("korean")), key=lambda p: -counts.get(p["no"], 0))
    targets = (korean + ranked)[:limit]
    added = 0
    for p in targets:
        try:
            vids = yt_get("search", {"part": "snippet", "q": p["name"], "type": "video", "order": "relevance",
                                     "publishedAfter": since, "maxResults": 3, "relevanceLanguage": "ko", "regionCode": "KR"})
        except Exception as e:  # noqa: BLE001
            print(f"[유튜브 실패] {p['name']}: {e}")
            if "403" in str(e):  # 할당량 초과 등
                break
            continue
        kept = 0
        for v in vids.get("items", []):
            vid, vtitle = v["id"].get("videoId"), clean(v["snippet"].get("title"))
            if not vid:
                continue
            try:
                cs = yt_get("commentThreads", {"part": "snippet", "videoId": vid, "order": "relevance",
                                               "maxResults": 10, "textFormat": "plainText"})
            except Exception:  # noqa: BLE001  댓글이 막힌 영상
                continue
            for c in cs.get("items", []):
                top = c["snippet"]["topLevelComment"]
                s = top["snippet"]
                text = clean(s.get("textDisplay") or s.get("textOriginal"))[:200]
                if not text or not is_clean(text):
                    continue
                try:
                    pub = datetime.fromisoformat(s["publishedAt"].replace("Z", "+00:00")).astimezone(KST)
                except (KeyError, ValueError):
                    continue
                mid = hashlib.sha1(f"{p['no']}|yt|{top['id']}".encode()).hexdigest()[:16]
                cur = db.execute("INSERT OR IGNORE INTO mentions VALUES(?,?,?,?,?,?,?,?)",
                                 (mid, p["no"], "youtube", f"https://www.youtube.com/watch?v={vid}&lc={top['id']}",
                                  text, f"유튜브 댓글: {vtitle}", pub.isoformat(), stamp))
                kept += cur.rowcount
        added += kept
        print(f"[유튜브] {p['name']}: 댓글 {kept}건 새로 저장")
    db.commit()
    return added


def load_targets() -> list[dict]:
    """직접 고른 60명(players.json) + 전체 명단(data/players.json)에서 자동으로 고른 선수"""
    players = json.loads(PLAYERS_PATH.read_text(encoding="utf-8"))
    for p in players:
        p["korean"] = p["no"] <= 9  # 직접 고른 60명 중 1~9번이 한국 선수
    try:
        roster = json.loads(ROSTER_PATH.read_text(encoding="utf-8"))["players"]
    except (OSError, ValueError, KeyError):
        return players
    have = {p["no"] for p in players}
    auto = []
    # [번호, 한글명, 영문명, 포지션, 나라, 팀, 리그, 출생연도, 키, 등번호]
    for row in roster:
        no, kr, _en, _pos, nat, club, lg, _b, _h, num = row[:10]
        if lg == "K리그2":
            continue  # K리그2는 비용 때문에 아직 반응 수집 대상이 아님
        if lg == "K리그1":
            # K리그는 등번호 1~30번만. 흔한 이름이 많아서 팀 지역명을 같이 검색 (예: "정승원" 서울)
            if no in have or not str(num).isdigit() or not 1 <= int(num) <= 30 or "," in kr:
                continue
            area = next((t for t in club.split() if t.upper() != "FC" and not t.isdigit()), club)
            auto.append({"no": no, "name": kr, "club": club, "auto": True, "korean": False,
                         "query": f'"{kr}" {area}', "must_any": [],
                         "exclude": ["배우", "드라마", "가수", "예능", "국회", "의원", "야구", "농구", "배구"]})
            continue
        if no in have or not re.search(r"[가-힣]", kr) or len(kr) > 20 or "," in kr:
            continue  # 한글 이름이 아직 없으면 한국 기사 검색이 안 됨
        korean = nat in ("KOR", "KORS")
        regular = club in BIG_CLUBS and str(num).isdigit() and 1 <= int(num) <= 35
        if not (korean or regular):
            continue
        auto.append({"no": no, "name": kr, "club": club, "auto": True, "korean": korean,
                     # 한국 선수는 흔한 이름이 많아 '축구'를 붙여 검색
                     "query": f'"{kr}" 축구' if korean else f'"{kr}"',
                     "must_any": [], "exclude": ["배우", "드라마", "가수", "예능", "국회", "의원"] if korean else []})
    print(f"[대상] 직접 고른 {len(players)}명 + 자동 {len(auto)}명")
    return players + auto


def collect(db: sqlite3.Connection, players: list[dict]) -> int:
    added = 0
    stamp = now_kst().isoformat()
    cutoff = now_kst() - timedelta(days=WINDOW_DAYS)
    fails = 0
    for p in players:
        name_keys = {p["name"].split()[-1], p["query"].split()[0].strip('"')}
        for source, fetch in SOURCES.items():
            try:
                items = fetch(p["query"])
                fails = 0
            except Exception as e:  # 한 선수·한 출처가 실패해도 나머지는 계속
                print(f"[수집 실패] {p['name']} / {source}: {e}")
                fails += 1
                if fails >= 5:  # 연달아 막히면 잠깐 쉬었다가 계속
                    print("[대기] 연속 실패, 60초 쉼")
                    time.sleep(60)
                    fails = 0
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
summary가 "유튜브 댓글"로 시작하면 title은 그 영상에 달린 팬 댓글이다. 이때 relevant는 댓글이 이 선수에 대한 말인지,
sentiment는 댓글 쓴 사람이 이 선수를 어떻게 보는지(칭찬 pos, 비판·조롱 neg)로 판단한다.
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

    raw_key = os.environ.get("ANTHROPIC_API_KEY", "")
    key = raw_key.strip()  # 붙여넣을 때 딸려 온 줄바꿈·공백 제거
    print(f"[확인] API 키: 길이 {len(key)}, 형식 {'정상' if key.startswith('sk-ant-') else '확인 필요'}, "
          f"앞뒤 공백·줄바꿈 {'있었음(제거함)' if key != raw_key else '없음'}")
    if not key:
        print("[분류 건너뜀] ANTHROPIC_API_KEY가 비어 있어요")
        return 0
    client = anthropic.Anthropic(api_key=key)
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
    fails = 0
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
                fails += 1
                if fails >= 3:
                    print("[분류 중단] 3번 연속 실패해서 멈춰요. 남은 기사는 다음 실행 때 분류해요.")
                    return labeled
                continue
            fails = 0
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
            print(f"[재시도] 연결 오류: {e!r} / 원인: {e.__cause__!r}")
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
           "tracked": sorted(p["no"] for p in players),  # 수집 대상 번호 (기사가 0건이어도 대상임을 표시)
           "players": {}}

    for p in players:
        rows = db.execute(
            """SELECT m.title, m.url, m.published_at, l.sentiment, l.topics, l.keywords
               FROM mentions m JOIN labels l ON l.mention_id = m.id
               WHERE m.player_no = ? AND l.relevant = 1 AND substr(m.published_at,1,10) >= ?
                 AND m.source != 'youtube'
               ORDER BY m.published_at DESC""",
            (p["no"], since),
        ).fetchall()
        # 커뮤니티(유튜브 댓글) 반응은 따로
        crow = db.execute(
            """SELECT m.title, m.url, l.sentiment FROM mentions m JOIN labels l ON l.mention_id = m.id
               WHERE m.player_no = ? AND l.relevant = 1 AND m.source = 'youtube' AND substr(m.published_at,1,10) >= ?
               ORDER BY m.published_at DESC""",
            (p["no"], since),
        ).fetchall()
        comm = None
        if crow:
            cs = Counter(r[2] for r in crow)
            comm = {"n": len(crow), "sentiment": {k: cs.get(k, 0) for k in ("pos", "neu", "neg")},
                    "top": [{"text": r[0], "url": r[1], "sentiment": r[2]} for r in crow if is_clean(r[0])][:3]}
        if not rows and comm:
            out["players"][str(p["no"])] = {"n": 0, "h24": 0, "sentiment": {"pos": 0, "neu": 0, "neg": 0},
                                            "daily": [], "topics": [], "keywords": [], "headlines": [], "comm": comm}
            continue
        if not rows and p.get("auto"):
            continue  # 자동 대상은 기사가 있을 때만 파일에 넣어 크기를 줄임
        per_day = Counter(r[2][:10] for r in rows)
        since24 = now_kst() - timedelta(hours=24)
        h24 = sum(1 for r in rows if datetime.fromisoformat(r[2]) >= since24)
        senti = Counter(r[3] for r in rows)
        topics = Counter(t for r in rows for t in json.loads(r[4]))
        keywords = Counter(k for r in rows for k in json.loads(r[5]))
        out["players"][str(p["no"])] = {
            "n": len(rows),
            "h24": h24,  # 최근 24시간 기사 수 (실시간 화제 순위용)
            "sentiment": {k: senti.get(k, 0) for k in ("pos", "neu", "neg")},
            "daily": [{"date": d, "n": per_day.get(d, 0)} for d in days],
            "topics": [t for t, _ in topics.most_common(3)],
            "keywords": [k for k, _ in keywords.most_common(5)],
            "headlines": [{"title": r[0], "url": r[1], "date": r[2][:10], "sentiment": r[3]} for r in rows[:3]],
        }
        if comm:
            out["players"][str(p["no"])]["comm"] = comm
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[집계] {OUT_PATH.name} 저장 ({len(players)}명)")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-llm", action="store_true", help="수집만 하고 분류·집계는 건너뜀")
    ap.add_argument("--aggregate", action="store_true", help="집계만 다시 실행")
    args = ap.parse_args()

    players = load_targets()
    db = open_db()
    if args.aggregate:
        aggregate(db, players)
        return
    added = collect(db, players)
    print(f"새 기사 {added}건")
    # 유튜브 댓글은 하루 한 번만 (마지막 수집 뒤 20시간이 지났을 때)
    last = db.execute("SELECT MAX(collected_at) FROM mentions WHERE source = 'youtube'").fetchone()[0]
    if not last or datetime.fromisoformat(last) < now_kst() - timedelta(hours=20):
        print(f"유튜브 댓글 {collect_youtube(db, players)}건")
    if args.no_llm:
        return
    classify(db, players)
    aggregate(db, players)


if __name__ == "__main__":
    main()
