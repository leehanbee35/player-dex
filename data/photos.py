"""선수 사진 찾기 (Wikidata → 위키미디어 공용 사진) → data/photos.json

1) Wikidata에 ESPN 선수 번호(P3681)가 적혀 있으면 그대로 연결
2) 없으면 축구 선수 + 사진 있음 + 생년월일이 같고 이름이 같은 사람을 찾음
결과: {"선수 번호": "md5 앞 두 글자/파일명"} → 페이지에서 250px 썸네일 주소로 바꿔 씀
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "photos.json"
SPARQL = "https://query.wikidata.org/sparql"
UA = "player-dex/1.0 (https://leehanbee35.github.io/player-dex/; github actions)"
OK_EXT = (".jpg", ".jpeg", ".png")


def sparql(q: str):
    url = SPARQL + "?format=json&query=" + urllib.parse.quote(q)
    for i in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/sparql-results+json"})
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.load(r)["results"]["bindings"]
        except Exception as e:  # noqa: BLE001
            print(f"[사진] 조회 재시도 {i + 1}: {e!r}")
            time.sleep(10 + i * 20)
    return None


def tokens(name: str) -> frozenset[str]:
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return frozenset(t for t in re.split(r"[^a-z]+", s) if t)


def file_key(img_url: str) -> str | None:
    name = urllib.parse.unquote(img_url.rsplit("/Special:FilePath/", 1)[-1]).replace(" ", "_")
    if not name.lower().endswith(OK_EXT):
        return None
    h = hashlib.md5(name.encode("utf-8")).hexdigest()
    return f"{h[:2]}/{name}"


def find_photos(roster: list[dict], no_of: dict[str, int]) -> dict[str, str] | None:
    """roster: [{id, en, dob(YYYY-MM-DD)}], no_of: ESPN id → 선수 번호"""
    out: dict[str, str] = {}

    rows = sparql("SELECT ?espn ?img WHERE { ?p wdt:P3681 ?espn ; wdt:P18 ?img . }")
    if rows is None:
        return None
    by_espn = {}
    for r in rows:
        k = file_key(r["img"]["value"])
        if k:
            by_espn.setdefault(r["espn"]["value"], k)
    for a in roster:
        k = by_espn.get(a["id"])
        if k and a["id"] in no_of:
            out[str(no_of[a["id"]])] = k
    print(f"[사진] ESPN 번호로 {len(out)}명")

    # 생년월일 + 이름으로 찾기 (2년씩 나눠서 조회)
    by_date: dict[str, list[tuple[frozenset[str], str]]] = {}
    for y in range(1978, 2012, 2):
        q = (
            "SELECT ?en ?b ?img WHERE { ?p wdt:P106 wd:Q937857 ; wdt:P18 ?img ; wdt:P569 ?b . "
            f'FILTER(?b >= "{y}-01-01T00:00:00Z"^^xsd:dateTime && ?b < "{y + 2}-01-01T00:00:00Z"^^xsd:dateTime) '
            # 대표 이름 + 다른 이름(별칭)까지: ESPN은 애칭, Wikidata는 본명인 경우가 많음
            '?p rdfs:label|skos:altLabel ?en . FILTER(LANG(?en) IN ("en", "mul")) }'
        )
        rows = sparql(q)
        if rows is None:
            print(f"[사진] {y}~{y + 1}년생 조회 실패, 건너뜀")
            continue
        for r in rows:
            k = file_key(r["img"]["value"])
            if k:
                by_date.setdefault(r["b"]["value"][:10], []).append((tokens(r["en"]["value"]), k))
        time.sleep(2)
    n0 = len(out)
    for a in roster:
        no = no_of.get(a["id"])
        if not no or str(no) in out or not a.get("dob"):
            continue
        cands = by_date.get(a["dob"], [])
        t = tokens(a["en"])
        files = {k for tt, k in cands if tt == t}
        if not files:  # 이름 일부만 같은 경우 (예: 브라질 선수 애칭)
            files = {k for tt, k in cands if t and (t <= tt or tt <= t)}
        if not files:  # 생년월일이 같고 3글자 이상 이름 단어(주로 성)가 겹치는 선수가 딱 한 명일 때
            files = {k for tt, k in cands if any(len(w) >= 3 for w in t & tt)}
        if len(files) == 1:
            out[str(no)] = files.pop()
    print(f"[사진] 이름·생년월일로 {len(out) - n0}명 → 모두 {len(out)}명")
    return out


def save(photos: dict[str, str] | None) -> None:
    if photos is None or len(photos) < 500:
        print("[사진] 결과가 너무 적어 이전 파일을 유지합니다")
        return
    OUT.write_text(json.dumps(photos, ensure_ascii=False, separators=(",", ":"), sort_keys=True), encoding="utf-8")
