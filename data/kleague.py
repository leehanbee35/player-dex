"""K리그1·K리그2 선수 명단 (한국어 위키백과 → Wikidata)

ESPN에는 K리그가 없어서 한국어 위키백과의 'YYYY년 K리그1' 문서에서 참가 구단을,
각 구단 문서의 '선수 명단'(클럽팀 선수명단 틀)에서 등번호·국적·포지션·한글 이름을 받음.
생년·키·영문 이름·사진은 선수 문서에 연결된 Wikidata에서 받음.
위키백과·Wikidata는 공식 API로 자유롭게 쓸 수 있음 (CC BY-SA / CC0).
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime

UA = "player-dex/1.0 (https://leehanbee35.github.io/player-dex/; github actions)"
WIKI = "https://ko.wikipedia.org/w/api.php"
WD = "https://www.wikidata.org/w/api.php"
LEAGUE = "K리그1"


def api(base: str, params: dict):
    url = base + "?" + urllib.parse.urlencode({**params, "format": "json", "formatversion": 2})
    for i in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001
            print(f"[K리그] 재시도 {i + 1}: {e!r}")
            time.sleep(3 + i * 5)
    return None


def wikitext(title: str) -> tuple[str, str] | None:
    d = api(WIKI, {"action": "parse", "page": title, "prop": "wikitext", "redirects": 1})
    if not d or "parse" not in d:
        return None
    return d["parse"]["title"], d["parse"]["wikitext"]


LINK = re.compile(r"\[\[([^\]|#]+)(?:\|([^\]]+))?\]\]")


def first_link(s: str) -> tuple[str, str] | None:
    for m in LINK.finditer(s):
        target = m.group(1).strip()
        if target.startswith(("파일:", "File:", "분류:")):
            continue
        shown = (m.group(2) or re.sub(r"\s*\(.*?\)\s*$", "", target)).strip()
        return target, shown
    return None


def clubs(league: str = LEAGUE) -> list[tuple[str, str]]:
    """[(구단 문서 제목, 화면 이름)] — 올해 시즌 문서가 없으면 작년 것.
    '참가 구단' 아래 첫 표(class="wikitable")를 읽음. K리그2 문서는 지도가 먼저 나옴"""
    y = datetime.now().year
    for year in (y, y - 1):
        r = wikitext(f"{year}년 {league}")
        if not r:
            continue
        w = r[1]
        i = w.find("참가 구단")
        i = w.find('class="wikitable', i) if i >= 0 else -1
        if i < 0:
            continue
        table = w[i: w.find("|}", i)]
        out = []
        for row in table.split("|-")[1:]:
            cell = row.strip().split("\n")[0]
            link = first_link(cell)
            if link:
                out.append(link)
        if len(out) >= 10:
            print(f"[K리그] {year}년 참가 구단 {len(out)}개")
            return out
    return []


def squad(club_title: str) -> list[dict]:
    r = wikitext(club_title)
    if not r:
        return []
    w = r[1]
    s = w.find("{{클럽팀 선수명단 시작")
    e = w.find("{{클럽팀 선수명단 끝", s)
    if s < 0 or e < 0:
        return squad_table(w)  # 틀 대신 일반 표로 적힌 구단 (예: FC 서울)
    out = []
    for m in re.finditer(r"\{\{클럽팀 선수명단\s*\|(.*?)\}\}\s*(?=\n|$)", w[s:e], re.S):
        body = m.group(1)
        f = {}
        # 이름 칸에는 [[..|..]]가 있어서 '|'로 바로 나누지 않고 키=값을 찾음
        for k in ("번호", "국적", "포지션", "이름"):
            mm = re.search(rf"{k}\s*=\s*(.*?)(?=\|\s*(?:번호|국적|포지션|이름|기타)\s*=|$)", body, re.S)
            f[k] = mm.group(1).strip() if mm else ""
        link = first_link(f["이름"])
        if not link:
            continue
        pos = f["포지션"].upper()[:2]
        out.append({"title": link[0], "kr": link[1], "num": re.sub(r"\D", "", f["번호"]),
                    "nat_ko": re.sub(r"\[\[|\]\]|\{\{.*?\}\}", "", f["국적"]).strip(),
                    "pos": pos if pos in ("GK", "DF", "MF", "FW") else "FW"})
    return out


POS_KO = {"골키퍼": "GK", "수비수": "DF", "미드필더": "MF", "공격수": "FW"}


def squad_table(w: str) -> list[dict]:
    """'선수 명단' 문단의 첫 표(배번·포지션·국적·이름)를 읽음"""
    i = w.find("선수 명단")
    s = w.find("{|", i) if i >= 0 else -1
    e = w.find("\n|}", s)
    if s < 0 or e < 0:
        return []
    out = []
    for row in w[s:e].split("|-")[1:]:
        cells = [c.strip() for c in re.split(r"\n\|(?!\})", "\n" + row.strip()) if c.strip()]
        if len(cells) < 4:
            continue
        num = re.sub(r"<[^>]+>|\D", "", cells[0])
        pm = re.search(r"\{\{(골키퍼|수비수|미드필더|공격수)\}\}", cells[1])
        nm = re.search(r"\{\{국기그림\|([^}|]+)", cells[2]) or re.search(r"\[\[([^\]|]+)", cells[2])
        link = first_link(cells[3])
        if not link:
            continue
        out.append({"title": link[0], "kr": link[1], "num": num, "nat_ko": nm.group(1).strip() if nm else "",
                    "pos": POS_KO.get(pm.group(1), "FW") if pm else "FW"})
    return out


def wikidata(titles: list[str]) -> dict[str, dict]:
    """위키백과 문서 제목 → {en, born, h, img}"""
    qid = {}
    for i in range(0, len(titles), 50):
        d = api(WIKI, {"action": "query", "prop": "pageprops", "ppprop": "wikibase_item",
                       "redirects": 1, "titles": "|".join(titles[i: i + 50])})
        if not d:
            continue
        q = d.get("query", {})
        back = {x["to"]: x["from"] for x in q.get("redirects", []) + q.get("normalized", [])}
        for p in q.get("pages", []):
            item = p.get("pageprops", {}).get("wikibase_item")
            if item:
                t = p["title"]
                qid[back.get(t, t)] = item
                qid[t] = item
        time.sleep(0.5)
    info: dict[str, dict] = {}
    ids = sorted(set(qid.values()))
    ent: dict[str, dict] = {}
    for i in range(0, len(ids), 50):
        d = api(WD, {"action": "wbgetentities", "ids": "|".join(ids[i: i + 50]), "props": "claims|labels",
                     "languages": "en"})
        if d:
            ent.update(d.get("entities", {}))
        time.sleep(0.5)

    def claim(e, pid):
        try:
            return e["claims"][pid][0]["mainsnak"]["datavalue"]["value"]
        except (KeyError, IndexError, TypeError):
            return None

    # Wikidata에 사진이 없으면 위키백과 문서의 대표 사진 (위키미디어 공용 = 자유 라이선스인 것만)
    page_img: dict[str, str] = {}
    for i in range(0, len(titles), 50):
        d = api(WIKI, {"action": "query", "prop": "pageimages", "piprop": "name|thumbnail", "pithumbsize": 250,
                       "redirects": 1, "titles": "|".join(titles[i: i + 50])})
        if not d:
            continue
        q = d.get("query", {})
        back = {x["to"]: x["from"] for x in q.get("redirects", []) + q.get("normalized", [])}
        for p in q.get("pages", []):
            src = (p.get("thumbnail") or {}).get("source", "")
            if p.get("pageimage") and "/wikipedia/commons/" in src:
                page_img[back.get(p["title"], p["title"])] = p["pageimage"]
        time.sleep(0.5)

    for t in titles:
        if t not in qid and t in page_img:
            info[t] = {"en": "", "born": 0, "h": 0, "img": page_img[t]}
    for t, q in qid.items():
        e = ent.get(q)
        if not e:
            continue
        born = claim(e, "P569")
        h = claim(e, "P2048")
        hv = 0
        if isinstance(h, dict):
            amt = abs(float(h.get("amount", 0)))
            hv = round(amt * 100) if amt < 3 else round(amt)  # 미터/센티미터
        img = claim(e, "P18")
        info[t] = {
            "en": e.get("labels", {}).get("en", {}).get("value", ""),
            "born": int(born["time"][1:5]) if isinstance(born, dict) and born.get("time", "")[1:5].isdigit() else 0,
            "h": hv if 140 <= hv <= 215 else 0,
            "img": img if isinstance(img, str) else page_img.get(t, ""),
        }
    return info


def photo_key(name: str) -> str | None:
    name = name.replace(" ", "_")
    if not name.lower().endswith((".jpg", ".jpeg", ".png")):
        return None
    return f"{hashlib.md5(name.encode('utf-8')).hexdigest()[:2]}/{name}"


def fetch(ids: dict, nat_code: dict[str, str], curated: list[dict], next_no: int, league: str = LEAGUE):
    """K리그(1 또는 2) 선수 행, 사진, 직접 입력 선수 갱신값, 다음 번호를 돌려줌.
    ids: 번호 고정표 (키 'kw:문서 제목'), nat_code: 한글 나라 이름 → 나라 코드"""
    rows, photos, overrides = [], {}, {}
    cl = clubs(league)
    if not cl:
        print(f"[{league}] 참가 구단을 못 찾음, 건너뜀")
        return rows, photos, overrides, next_no
    squads = []
    for title, shown in cl:
        sq = squad(title)
        print(f"[K리그] {shown}: {len(sq)}명")
        squads.append((shown, sq))
        time.sleep(0.5)
    info = wikidata(sorted({p["title"] for _, sq in squads for p in sq}))
    cur_by_name = {c["kr"]: c for c in curated}
    for club, sq in squads:
        for p in sq:
            d = info.get(p["title"], {})
            c = cur_by_name.get(p["kr"])
            if c and c.get("born") == d.get("born"):  # 직접 입력한 선수 (예: 조현우)
                overrides[c["no"]] = [club, league, "w:" + p["title"], p["num"]]
                continue
            key = "kw:" + p["title"]
            if key not in ids:
                ids[key] = next_no
                next_no += 1
            no = ids[key]
            rows.append([no, p["kr"], d.get("en", ""), p["pos"], nat_code.get(p["nat_ko"], ""), club, league,
                         d.get("born", 0), d.get("h", 0), p["num"], "w:" + p["title"]])  # w:위키백과 문서 → 상세 화면 경력 기록
            k = photo_key(d.get("img", ""))
            if k:
                photos[str(no)] = k
    print(f"[{league}] 선수 {len(rows)}명, 사진 {len(photos)}명")
    return rows, photos, overrides, next_no
