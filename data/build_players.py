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
NAMES = HERE / "names.json"  # 직접 고친 한글 이름
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
# ESPN 나라 코드 중 페이지(직접 입력 60명)와 다른 것 → 같은 코드로 맞춤
NAT_ALIAS = {"KORS": "KOR", "MOR": "MAR"}


def clean_ko(v: str) -> str:
    """'선수 정우영, 대한민국, 우니온 베를린'처럼 설명째 돌아온 표기에서 이름만 남김"""
    v = re.sub(r"^(선수|축구팀|나라)\s+", "", v.strip())
    return re.split(r"\s*[,(]", v)[0].strip()


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
                cc = NAT_ALIAS.get(cc.upper(), cc)
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
                    "dob": dob[:10],
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


TRANSLATE_HEAD = (
    "각 줄은 '키<TAB>설명'입니다. 설명 맨 앞의 고유명사(선수 이름, 팀 이름, 나라 이름) 하나만 한글 표기로 바꿔 같은 키와 함께 돌려주십시오. "
    "괄호 안 국적·소속은 참고용이니 넣지 마십시오. 예: '선수 Son Heung-min (Korea Republic, LAFC)' → '손흥민'.\n\n"
)
REVIEW_HEAD = (
    "각 줄은 '키<TAB>영문 이름 (국적, 소속팀)<TAB>지금 한글 표기'입니다. 지금 한글 표기를 한국 스포츠 언론(네이버 스포츠, 스포티비 등)에서 "
    "실제로 가장 많이 쓰는 표기와 비교해, 다르면 고친 표기를, 맞으면 지금 표기를 그대로 같은 키와 함께 돌려주십시오. "
    "예: 'Iliman Ndiaye' 릴리만 은디아예 → 일리만 은디아예, 'Gerónimo Rulli' 헤로니모 훌리 → 헤로니모 룰리. 이름만, 설명 없이 넣으십시오.\n\n"
)


def translate(todo: list[tuple[str, str]], ko: dict, head: str = TRANSLATE_HEAD, done=None) -> None:
    """todo: [(캐시 키, 설명 문자열)] → ko[키] = 한글. done(키 목록): 한 묶음을 마칠 때마다 호출"""
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
        prompt = head + lines
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
            v = clean_ko(it["ko"])
            if it["k"] in want and v and re.search(r"[가-힣]", v) and len(v) <= 40:
                ko[it["k"]] = v
                got += 1
        fails = 0
        if done:
            done([k for k, _ in batch])
        print(f"[한글] {i + len(batch)}/{len(todo)} (이번 {got}개)")
        KO.write_text(json.dumps(ko, ensure_ascii=False, indent=0, sort_keys=True), encoding="utf-8")


def main() -> None:
    curated = load(CURATED, [])
    ids: dict[str, int] = load(IDS, {})
    ko: dict[str, str] = load(KO, {})
    ko = {k: clean_ko(v) for k, v in ko.items() if clean_ko(v)}  # 예전에 설명째 저장된 표기 정리

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

    # 주요 선수 이름 재검수 (한 번 검수한 선수는 'r:번호'로 표시해 다시 안 함)
    REVIEW_LEAGUES = {"프리미어리그", "라리가", "분데스리가", "세리에 A", "리그 1"}
    review = [
        ("p:" + a["id"], "%s (%s, %s)\t%s" % (a["en"], a["nat_en"], a["club"], ko["p:" + a["id"]]))
        for a in roster
        if a["id"] not in cur_map and f"p:{a['id']}" in ko and f"r:{a['id']}" not in ko
        and (a["league"] in REVIEW_LEAGUES or a["nat"] == "KOR")
    ]
    print(f"[검수] 주요 선수 이름 {len(review)}개")

    def mark(keys):
        for k in keys:
            ko["r:" + k[2:]] = "1"

    translate(review, ko, REVIEW_HEAD, mark)
    KO.write_text(json.dumps(ko, ensure_ascii=False, indent=0, sort_keys=True), encoding="utf-8")

    # 직접 고친 이름(data/names.json: {"선수 번호": "한글 이름"})이 가장 우선
    fixed = {int(k): v for k, v in load(NAMES, {}).items() if str(k).isdigit() and v}

    players = []
    overrides = {}
    for a in roster:
        club = ko.get(f"t:{a['club']}", a["club"])
        if a["id"] in cur_map:
            overrides[cur_map[a["id"]]] = [club, a["league"], a["id"]]
            continue
        players.append([
            ids[a["id"]],
            fixed.get(ids[a["id"]]) or ko.get(f"p:{a['id']}", a["en"]),
            a["en"], a["pos"], a["nat"], club, a["league"], a["born"], a["h"], a["num"],
            a["id"],  # ESPN 선수 번호: 상세 화면에서 이번 시즌 기록을 받을 때 씀
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

    # 사진 (위키미디어 공용). 실패해도 명단은 그대로 저장됨
    import photos

    photos.save(photos.find_photos(roster, {**ids, **cur_map}))


if __name__ == "__main__":
    main()
