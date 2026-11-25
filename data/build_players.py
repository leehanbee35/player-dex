"""전체 선수 명단 만들기 (ESPN 공개 명단 → data/players.json)

- 유럽 주요 리그 + 챔피언십·MLS·사우디·J리그·브라질 등 리그별 모든 팀의 현재 명단을 받음 (키 필요 없음)
- 선수 번호는 data/ids.json에 고정 저장 (한 번 받은 번호는 바뀌지 않음 → 댓글이 다른 선수로 옮겨 가지 않음)
- 직접 입력한 60명(1~60번)은 같은 선수를 찾아 클럽·리그만 최신으로 덮어씀
- 한글 이름·팀명·나라명은 Claude로 한 번만 만들고 data/ko.json에 저장 (ANTHROPIC_API_KEY가 없으면 영문 그대로)

실행: python data/build_players.py
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import unicodedata
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "players.json"
IDS = HERE / "ids.json"
KO = HERE / "ko.json"
CURATED = HERE / "curated.json"
MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-5-5")
API = "https://site.api.espn.com/apis/site/v2/sports/soccer"
UA = "player-dex/1.0 (+https://github.com)"

# (ESPN 리그 코드, 화면에 쓰는 리그 이름). 직접 입력한 60명과 같은 이름을 씀
LEAGUES = [
    ("eng.1", "프리미어리그"),
    ("esp.1", "라리가"),
    ("ger.1", "분데스리가"),
    ("ita.1", "세리에 A"),
    ("fra.1", "리그 1"),
    ("ned.1", "에레디비시"),
    ("por.1", "프리메이라리가"),
    ("eng.2", "챔피언십"),
    ("bel.1", "벨기에 프로리그"),
    ("sco.1", "스코티시 프리미어십"),
    ("tur.1", "쉬페르리그"),
    ("ksa.1", "사우디 프로리그"),
    ("usa.1", "MLS"),
    ("jpn.1", "J1리그"),
    ("bra.1", "브라질 세리에 A"),
    ("arg.1", "아르헨티나 리가"),
]
FIRST_NEW_NO = 61


def get(url: str, tries: int = 4):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001
            if i == tries - 1:
                print(f"[실패] {url}: {e!r}")
                return None
            time.sleep(2 + i * 3)


def pos_of(a: dict) -> str:
    p = a.get("position") or {}
    s = f"{p.get('abbreviation', '')} {p.get('name', '')}".lower()
    if "goal" in s or s.startswith("g"):
        return "GK"
    if "def" in s or s.startswith("d"):
        return "DF"
    if "mid" in s or s.startswith("m"):
        return "MF"
    return "FW"


def norm_tokens(name: str) -> frozenset[str]:
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return frozenset(t for t in re.split(r"[^a-z]+", s) if t)


def load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def fetch_all() -> list[dict]:
    seen: dict[str, dict] = {}
    for slug, league in LEAGUES:
        t = get(f"{API}/{slug}/teams")
        if not t:
            continue
        teams = [x["team"] for x in t["sports"][0]["leagues"][0]["teams"]]
        n0 = len(seen)
        for team in teams:
            r = get(f"{API}/{slug}/teams/{team['id']}/roster")
            time.sleep(0.15)
            if not r:
                continue
            for a in r.get("athletes", []):
                aid = str(a.get("id", ""))
                if not aid or aid in seen:
                    continue
                dob = a.get("dateOfBirth") or ""
                cc = (a.get("citizenshipCountry") or {}).get("abbreviation") or ""
                h = a.get("height") or 0
                seen[aid] = {
                    "id": aid,
                    "en": a.get("displayName") or a.get("fullName") or "",
                    "pos": pos_of(a),
                    "nat": cc.upper(),
                    "nat_en": a.get("citizenship") or cc,
                    "flag": (a.get("flag") or {}).get("href", ""),
                    "club": team.get("displayName", ""),
                    "league": league,
                    "born": int(dob[:4]) if dob[:4].isdigit() else 0,
                    "h": round(h * 2.54) if h else 0,
                    "num": a.get("jersey") or "",
                }
        print(f"[명단] {league}: 팀 {len(teams)}개, 선수 {len(seen) - n0}명")
    return list(seen.values())


# ---------- 한글 표기 (Claude, 한 번 만든 것은 ko.json에 저장) ----------

SYSTEM = (
    "축구 관련 영문 고유명사를 한국 스포츠 언론(네이버 스포츠, 스포티비 등)에서 실제로 쓰는 한글 표기로 바꿉니다. "
    "선수 이름은 국적을 참고해 원어 발음에 가깝게, 널리 쓰이는 표기가 있으면 그것을 씁니다(예: Son Heung-min → 손흥민, "
    "Erling Haaland → 엘링 홀란, Vinícius Júnior → 비니시우스 주니오르). 일본·중국 선수는 성-이름 순서(예: Takefusa Kubo → 쿠보 다케후사). "
    "팀 이름은 한국에서 부르는 이름(예: Manchester City → 맨체스터 시티, Wolverhampton Wanderers → 울버햄튼, Bayern Munich → 바이에른 뮌헨, "
    "Paris Saint-Germain → 파리 생제르맹, Internazionale → 인테르). 나라 이름은 한국어 국명(예: Korea Republic → 대한민국, England → 잉글랜드)."
)
SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"k": {"type": "string"}, "ko": {"type": "string"}},
                "required": ["k", "ko"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}


def translate(todo: list[tuple[str, str]], ko: dict) -> None:
    """todo: [(캐시 키, 설명 문자열)] → ko[키] = 한글"""
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not todo:
        return
    if not key:
        print(f"[한글] ANTHROPIC_API_KEY 없음: {len(todo)}개는 영문으로 둠")
        return
    import anthropic

    client = anthropic.Anthropic(api_key=key)
    fails = 0
    for i in range(0, len(todo), 120):
        batch = todo[i : i + 120]
        lines = "\n".join(f"{k}\t{desc}" for k, desc in batch)
        prompt = (
            "각 줄은 '키<TAB>설명'입니다. 설명의 고유명사를 한글 표기로 바꿔 같은 키와 함께 돌려주십시오. "
            "모든 키를 빠짐없이, 한글 표기만(괄호·설명 없이) 넣으십시오.\n\n" + lines
        )
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
            print(f"[대기] 요청 한도, {wait}초")
            time.sleep(wait)
            continue
        except anthropic.APIStatusError as e:
            print(f"[한글 실패] {e.status_code}: {e.message}")
            fails += 1
            if fails >= 3 or "credit balance is too low" in str(e.message):
                break
            continue
        except anthropic.APIConnectionError as e:
            print(f"[한글 실패] 연결 오류: {e!r}")
            fails += 1
            if fails >= 3:
                break
            continue
        if res.stop_reason in ("refusal", "max_tokens"):
            print(f"[한글 건너뜀] stop_reason={res.stop_reason}")
            continue
        text = next((b.text for b in res.content if b.type == "text"), "")
        try:
            items = json.loads(text)["items"]
        except (ValueError, KeyError):
            print("[한글 실패] JSON 파싱 오류")
            continue
        want = {k for k, _ in batch}
        got = 0
        for it in items:
            v = it["ko"].strip()
            if it["k"] in want and v and re.search(r"[가-힣]", v) and len(v) <= 40:
                ko[it["k"]] = v
                got += 1
        fails = 0
        print(f"[한글] {i + len(batch)}/{len(todo)} (이번 {got}개)")
        KO.write_text(json.dumps(ko, ensure_ascii=False, indent=0, sort_keys=True), encoding="utf-8")


def main() -> None:
    curated = load(CURATED, [])
    ids: dict[str, int] = load(IDS, {})
    ko: dict[str, str] = load(KO, {})

    roster = fetch_all()
    if len(roster) < 1000:
        print(f"[중단] 선수가 {len(roster)}명밖에 안 됨. 이전 데이터를 유지합니다")
        sys.exit(0 if OUT.exists() else 1)

    # 직접 입력한 60명과 같은 선수 찾기: 이름 토큰이 같거나 포함 관계 + 출생연도 같음
    by_year: dict[int, list[dict]] = {}
    for a in roster:
        by_year.setdefault(a["born"], []).append(a)
    cur_map: dict[str, int] = {}
    for c in curated:
        ct = norm_tokens(c["en"])
        cands = [a for a in by_year.get(c["born"], []) if norm_tokens(a["en"]) == ct]
        if not cands:
            cands = [a for a in by_year.get(c["born"], []) if ct <= norm_tokens(a["en"]) or norm_tokens(a["en"]) <= ct]
            cands = [a for a in cands if len(norm_tokens(a["en"]) & ct) >= 1 and len(ct) >= 1]
        if len(cands) == 1:
            cur_map[cands[0]["id"]] = c["no"]
    print(f"[직접 입력 60명] 명단에서 {len(cur_map)}명 찾음")

    # 번호 고정: 처음 보는 선수만 새 번호
    nxt = max([FIRST_NEW_NO - 1, *ids.values()]) + 1
    for a in sorted(roster, key=lambda a: (LEAGUES.index(next(l for l in LEAGUES if l[1] == a["league"])), a["club"], a["en"])):
        if a["id"] in cur_map or a["id"] in ids:
            continue
        ids[a["id"]] = nxt
        nxt += 1
    IDS.write_text(json.dumps(ids, indent=0, sort_keys=True), encoding="utf-8")

    # 한글 표기 채우기 (직접 입력한 60명의 이름은 페이지에 있는 한글을 그대로 씀)
    todo: list[tuple[str, str]] = []
    nat_names = {}
    for a in roster:
        if a["nat"]:
            nat_names[a["nat"]] = a["nat_en"]
    for code, name in sorted(nat_names.items()):
        if f"c:{code}" not in ko:
            todo.append((f"c:{code}", f"나라 {name}"))
    for club in sorted({a["club"] for a in roster}):
        if f"t:{club}" not in ko:
            todo.append((f"t:{club}", f"축구팀 {club}"))
    for a in roster:
        if a["id"] not in cur_map and f"p:{a['id']}" not in ko:
            todo.append((f"p:{a['id']}", f"선수 {a['en']} ({a['nat_en']}, {a['club']})"))
    print(f"[한글] 새로 만들 표기 {len(todo)}개")
    translate(todo, ko)
    KO.write_text(json.dumps(ko, ensure_ascii=False, indent=0, sort_keys=True), encoding="utf-8")

    players = []
    overrides = {}
    for a in roster:
        club = ko.get(f"t:{a['club']}", a["club"])
        if a["id"] in cur_map:
            overrides[cur_map[a["id"]]] = [club, a["league"]]
            continue
        players.append([
            ids[a["id"]],
            ko.get(f"p:{a['id']}", a["en"]),
            a["en"], a["pos"], a["nat"], club, a["league"], a["born"], a["h"], a["num"],
        ])
    players.sort(key=lambda r: r[0])
    nat = {code: ko.get(f"c:{code}", name) for code, name in nat_names.items()}
    flags = {}
    for a in roster:
        m = re.search(r"/countries/500/([a-z0-9]+)\.png", a["flag"] or "")
        if a["nat"] and m:
            flags[a["nat"]] = m.group(1)
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="minutes"),
        "source": "ESPN",
        "leagues": [l for _, l in LEAGUES],
        "nat": nat,
        "flag": flags,
        "curated": overrides,
        "players": players,
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"[완료] 선수 {len(players)}명 + 직접 입력 {len(overrides)}명 갱신 → {OUT.name}")


if __name__ == "__main__":
    main()
