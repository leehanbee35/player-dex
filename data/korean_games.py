"""해외 리그 한국 선수의 다음 경기·최근 경기 → data/games.json (첫 화면 "오늘 뛰는 한국 선수")

data/players.json에서 국적이 대한민국이고 ESPN 선수 번호가 있는 선수(직접 입력한 1~9번 포함)를 골라
ESPN 선수 개요의 다음 경기(nextGame)와 최근 경기 기록(gameLog)을 받음. 반응 수집과 함께 3시간마다 실행.
"""

from __future__ import annotations

import json
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "games.json"
UA = "player-dex/1.0 (+https://leehanbee35.github.io/player-dex/)"
API = "https://site.web.api.espn.com/apis/common/v3/sports/soccer/athletes/{}/overview"


def get(url: str):
    for i in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001
            if i == 2:
                print(f"[경기] 실패 {url}: {e!r}")
            time.sleep(2)
    return None


def main() -> None:
    data = json.loads((HERE / "players.json").read_text(encoding="utf-8"))
    targets = []  # (번호, ESPN 번호)
    for no, espn in ((int(k), v[2] if len(v) > 2 else "") for k, v in data.get("curated", {}).items()):
        if 1 <= no <= 9 and espn and not str(espn).startswith("w:"):
            targets.append((no, str(espn)))
    for r in data["players"]:
        espn = r[10] if len(r) > 10 else ""
        if r[4] == "KOR" and espn and not str(espn).startswith("w:"):
            targets.append((r[0], str(espn)))
    now = datetime.now(timezone.utc)
    nxt, last = [], []
    for no, espn in targets:
        d = get(API.format(espn))
        time.sleep(0.3)
        if not d:
            continue
        lg = (d.get("nextGame") or {}).get("league") or {}
        ev = (lg.get("events") or [None])[0]
        if ev and ev.get("date"):
            when = datetime.fromisoformat(ev["date"].replace("Z", "+00:00"))
            if now - timedelta(hours=3) <= when <= now + timedelta(hours=48):
                cs = {c.get("homeAway"): (c.get("displayName") or c.get("team", {}).get("displayName", "")) for c in ev.get("competitors", [])}
                nxt.append({"no": no, "date": ev["date"], "home": cs.get("home", ""), "away": cs.get("away", ""),
                            "league": lg.get("name", "")})
        gl = d.get("gameLog") or {}
        st = (gl.get("statistics") or [None])[0]
        if st and st.get("events"):
            x = st["events"][0]
            e = (gl.get("events") or {}).get(x.get("eventId"), {})
            if e.get("gameDate"):
                when = datetime.fromisoformat(e["gameDate"].replace("Z", "+00:00"))
                if now - when <= timedelta(hours=36):
                    names = st.get("names", [])
                    val = lambda n: int(x["stats"][names.index(n)]) if n in names and str(x["stats"][names.index(n)]).isdigit() else 0
                    last.append({"no": no, "date": e["gameDate"], "opp": (e.get("opponent") or {}).get("displayName", ""),
                                 "atVs": e.get("atVs", ""), "score": e.get("score", ""), "result": e.get("gameResult", ""),
                                 "g": val("totalGoals"), "a": val("goalAssists"),
                                 "role": x["stats"][names.index("appearances")] if "appearances" in names else ""})
    nxt.sort(key=lambda g: g["date"])
    last.sort(key=lambda g: g["date"], reverse=True)
    OUT.write_text(json.dumps({"generated_at": now.isoformat(timespec="minutes"), "next": nxt, "last": last},
                              ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"[경기] 한국 선수 {len(targets)}명 중 48시간 안 경기 {len(nxt)}건, 36시간 안 결과 {len(last)}건")


if __name__ == "__main__":
    main()
