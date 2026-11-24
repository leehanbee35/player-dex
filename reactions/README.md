# 선수 반응 수집기 (1단계: 뉴스)

하루 두 번 선수별 최근 7일 기사 제목을 모아 Claude로 분류하고, 도감 페이지가 읽는 `reactions.json`을 만듭니다.

## 출처

| 출처 | 키 | 비고 |
|---|---|---|
| 구글 뉴스 RSS (한국어) | 필요 없음 | 기본 출처. 공식 API가 아니라 형식이 바뀌거나 막힐 수 있음 |
| 네이버 검색 API | `NAVER_CLIENT_ID`, `NAVER_CLIENT_SECRET` | 선택. 키가 있을 때만 함께 수집. 2026-11 기준 새 앱 등록 화면에서 "검색"을 고를 수 없었음 |

같은 기사가 두 출처에 모두 나오면 제목으로 한 번만 저장합니다.

## 파일

| 파일 | 하는 일 |
|---|---|
| `players.json` | 선수별 검색어와 거르기 규칙 (`exclude`: 있으면 버림, `must_any`: 네이버 기사에만 적용) |
| `collect.py` | 수집 → 분류 → 집계 |
| `reactions.db` | 모은 기사와 분류 결과 (SQLite, 자동 생성) |
| `reactions.json` | 최근 7일 집계 결과 (도감이 읽음) |
| `../.github/workflows/reactions.yml` | GitHub Actions 일정 (06:00, 18:00 KST) |

## 준비

1. Claude API 키 준비 (console.anthropic.com)
2. GitHub 저장소 → Settings → Secrets and variables → Actions 에 `ANTHROPIC_API_KEY` 등록
3. Actions 탭에서 "선수 반응 수집"을 수동 실행해 첫 데이터를 만듭니다.

## 모델과 비용

- 기본 모델은 `claude-opus-5-5`(effort low)입니다.
- 헤드라인 분류만 하므로 `CLAUDE_MODEL=claude-haiku-5-5`로 바꾸면 비용이 크게 줄어듭니다.
- 제목만 보내고, 이미 분류한 기사는 다시 보내지 않습니다.

## 선수 추가

`players.json`에 한 줄 추가하면 됩니다. `no`는 도감 번호와 같아야 합니다.
이름이 흔한 선수는 `query`에 클럽 이름을 붙이고 `exclude`에 배우, 정치인 같은 단어를 넣으세요.
동명이인 기사가 섞여도 Claude가 관련 없음(`relevant: false`)으로 걸러서 집계에서 빠집니다.

## 지키는 선

- 기사 본문은 저장하지 않습니다. 제목, 언론사, 링크만 저장합니다.
- 화면에는 집계와 헤드라인 3개만 보여주고 원문으로 연결합니다.
