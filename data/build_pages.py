"""검색 엔진용 선수 페이지(p/번호.html)와 sitemap.xml 만들기

도감 화면은 자바스크립트로 그려서 검색 엔진이 선수 정보를 잘 못 읽음.
반응을 수집하는 선수마다 이름·팀·국적·최근 뉴스 반응·대표 기사 제목이 글자로 들어간 가벼운 페이지를 만들고,
"도감에서 보기" 링크로 본 화면(#v-d-번호)에 연결함. 배포할 때마다 새로 만들고 저장소에는 올리지 않음.

실행: python data/build_pages.py
"""

from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SITE = "https://leehanbee35.github.io/player-dex/"
OUT_DIR = ROOT / "p"


def load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def main() -> None:
    data = load(ROOT / "data" / "players.json", {})
    react = load(ROOT / "reactions" / "reactions.json", {})
    curated = {c["no"]: c for c in load(ROOT / "data" / "curated.json", [])}
    nat = data.get("nat", {})
    nat.setdefault("KOR", "대한민국")
    year = datetime.now(timezone.utc).year

    people = {}  # 번호 → (한글, 영문, 팀, 리그, 나라 코드, 출생연도)
    for r in data.get("players", []):
        people[r[0]] = (r[1], r[2], r[5], r[6], r[4], r[7])
    for no, c in curated.items():
        ov = data.get("curated", {}).get(str(no), [])
        people[no] = (c["kr"], c["en"], ov[0] if ov else "", ov[1] if len(ov) > 1 else "", "", c.get("born", 0))

    tracked = set(react.get("tracked", [])) | set(curated)
    rp = react.get("players", {})
    OUT_DIR.mkdir(exist_ok=True)
    esc = lambda s: html.escape(str(s or ""), quote=True)
    urls = [SITE]
    for no in sorted(tracked):
        if no not in people:
            continue
        kr, en, club, league, code, born = people[no]
        r = rp.get(str(no)) or {}
        n, s = r.get("n", 0), r.get("sentiment") or {}
        tot = sum(s.get(k, 0) for k in ("pos", "neu", "neg")) or 1
        pct = {k: round(s.get(k, 0) / tot * 100) for k in ("pos", "neu", "neg")}
        who = " · ".join(x for x in (club, league, nat.get(code, ""), f"{year - born}세" if born else "") if x)
        mood = f"최근 {react.get('window_days', 7)}일 기사 {n}건 · 긍정 {pct['pos']}% · 부정 {pct['neg']}%" if n else "최근 기사 반응 수집 중"
        desc = f"{kr}({en}) {who}. {mood}. 풋볼 도감에서 시즌 기록·경기·팬 댓글을 볼 수 있습니다."
        heads = "".join(
            f'<li><a href="{esc(h.get("url"))}" rel="nofollow noopener" target="_blank">{esc(h.get("title"))}</a></li>'
            for h in (r.get("headlines") or [])[:5]
        )
        page = f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(kr)} 반응·기록 | 풋볼 도감</title>
<meta name="description" content="{esc(desc)}">
<link rel="canonical" href="{SITE}p/{no}.html">
<style>body{{margin:0;font:15px/1.6 system-ui,-apple-system,"Segoe UI","Noto Sans KR",sans-serif;background:#0e1424;color:#e8ecf5}}
main{{max-width:720px;margin:0 auto;padding:28px 18px}}h1{{margin:0 0 4px;font-size:26px}}.sub{{color:#99a4bd;margin:0 0 18px}}
a{{color:#8cb8ff}}.go{{display:inline-block;margin:6px 0 22px;padding:10px 18px;border-radius:999px;background:#2fbf71;color:#0b1a12;font-weight:700;text-decoration:none}}
ul{{padding-left:20px}}footer{{color:#99a4bd;font-size:12px;margin-top:28px}}</style></head>
<body><main>
<h1>{esc(kr)}</h1><p class="sub">{esc(en)} · {esc(who)}</p>
<p>{esc(mood)}</p>
<a class="go" href="../#v-d-{no}">풋볼 도감에서 보기</a>
{f"<h2>최근 기사</h2><ul>{heads}</ul>" if heads else ""}
<footer>풋볼 도감 · 선수 명단 ESPN·위키백과 · 뉴스 반응은 구글 뉴스 기사를 자동 분류한 결과입니다.</footer>
</main></body></html>
"""
        (OUT_DIR / f"{no}.html").write_text(page, encoding="utf-8")
        urls.append(f"{SITE}p/{no}.html")

    today = datetime.now(timezone.utc).date().isoformat()
    (ROOT / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "".join(f"<url><loc>{u}</loc><lastmod>{today}</lastmod></url>\n" for u in urls)
        + "</urlset>\n",
        encoding="utf-8",
    )
    print(f"[페이지] 선수 페이지 {len(urls) - 1}개, sitemap.xml")


if __name__ == "__main__":
    main()
