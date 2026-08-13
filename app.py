# -*- coding: utf-8 -*-
r"""
app.py — 세탁공장 컴플레인 티켓 시스템 v1

구조 (요청이 들어오면):
  브라우저 → FastAPI(이 파일의 함수들) → DB → HTML(templates\) → 브라우저
  DB = 로컬 개발은 SQLite(db.sqlite3), 배포는 Postgres(DATABASE_URL 환경변수가 있으면 자동 전환 — v2 이관)

화면 5개:
  /            공장 대시보드 — 미처리 티켓이 위에, 15초마다 자동 새로고침 (공장 모니터용)
  /new         컴플레인 접수 폼
  /c/{id}      티켓 상세 — 조치 일지(타임라인) + 지시·처리·상태 변경
  /me          담당자 선택 (로그인 대신 — v1은 증명용이라 이름 선택으로 충분)
  /me/{id}     내 티켓 — 나에게 배정된 것만, 확인 버튼 (기사가 폰으로 보는 화면)

원칙:
  - 조치 기록(actions)은 append-only — 한 번 쓰면 안 고친다. 수정 대신 새 기록을 쌓는다.
  - SMS 발송은 v2 — notify() 함수가 그 자리다. 지금은 기록만 하고 아무것도 안 보낸다.

실행:  .venv\Scripts\python -m uvicorn app:app --reload
"""

import base64
import io
import json
import os
import sqlite3
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from PIL import Image, ImageOps        # 채팅 사진 첨부(이미지 1단계) — 축소·회전 보정·재인코딩에 쓴다

BASE = Path(__file__).parent
DB_PATH = BASE / "db.sqlite3"
UPLOAD_DIR = BASE / "uploads"          # 올라온 사진이 저장되는 곳 (DB에는 파일명만)
UPLOAD_DIR.mkdir(exist_ok=True)

app = FastAPI(title="laundry-ticket")
templates = Jinja2Templates(directory=str(BASE / "templates"))
# /uploads/파일명 주소로 저장된 사진을 브라우저에 내보낸다
app.mount("/uploads", StaticFiles(directory=str(UPLOAD_DIR)), name="uploads")

# ── 한국어 표시용 이름표 (DB에는 영문 코드로 저장하고, 화면에서만 한국어로) ──
TYPE_LABEL = {"quality": "세탁 품질", "delivery": "배송", "etc": "기타"}
# 상태 new의 표시명은 「확인 대기」 — 예전 이름 「접수」는 등록 동작(접수하기·오늘 접수)과
# 낱말이 겹쳐 같은 화면에서 다른 숫자를 세는 혼란을 만들었다 (2026-08-10 UI 점검 지적).
# '접수'는 이제 동작(KIND_LABEL의 register)에만 쓴다.
STATUS_LABEL = {"new": "확인 대기", "acked": "확인됨", "working": "처리중", "done": "완료"}
# 'ack'는 「확인」이었으나 상태명 「확인 대기/확인됨」과 한 끗 차이라 「지시 확인」으로 (재검증 지적)
# 'move'는 현황판 카드 이동 전용 — 사람의 조치(처리 보고)와 기계적 이동을 일지에서 구분한다 (2라운드)
KIND_LABEL = {"register": "접수", "instruct": "지시", "ack": "지시 확인",
              "work": "처리 보고", "done": "완료 처리", "note": "메모", "move": "상태 이동"}
ROLE_LABEL = {"owner": "본사", "manager": "공장장", "factory": "공장", "driver": "배송기사"}


# 한국 시간 — 배포 서버(Render)는 UTC라서 그냥 now()를 쓰면 9시간 이르게 찍힌다(배포 검증에서 발견).
# KST는 서머타임이 없어 고정 +9가 항상 맞다 — 시간대 데이터베이스 없이도 안전.
KST = timezone(timedelta(hours=9))


def now() -> str:
    """지금 시각(한국 기준)을 'YYYY-MM-DD HH:MM' 글자로. DB에 시각은 전부 이 형식으로 넣는다."""
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M")


# ── DB 백엔드 선택 (v2: 무료 Postgres 이관, 2026-08-10) ──────────────────
# DATABASE_URL 환경변수가 있으면 Postgres(배포·Neon), 없으면 SQLite(로컬 개발).
# 왜 둘 다 지원하나: 로컬은 파일 하나(db.sqlite3)로 간편하게 돌리고,
# 배포는 서버 재시작마다 데이터가 날아가던 문제(무료 서버 디스크 비영속)를 외부 DB로 푼다.
DATABASE_URL = os.environ.get("DATABASE_URL", "")
IS_PG = bool(DATABASE_URL)
if IS_PG:
    import psycopg      # Postgres 드라이버 — 로컬 SQLite만 쓸 때는 설치 없이도 돌게 조건부로 불러온다


class PgRow:
    """Postgres 결과 한 줄을 sqlite3.Row처럼 쓰게 하는 껍데기.
    sqlite3.Row는 row[0](번호)도 row["name"](열 이름)도 되는데 기존 코드가 둘 다 쓰므로,
    Postgres 쪽도 똑같이 맞춰야 본문 코드를 안 고친다. 화면(Jinja)의 t.id 접근도
    「속성 실패 → 대괄호 접근」으로 넘어가는 Jinja 규칙 덕에 이걸로 동작한다."""
    __slots__ = ("_cols", "_vals")

    def __init__(self, cols, vals):
        self._cols = cols     # 열 이름 목록
        self._vals = vals     # 값 목록 (열 순서대로)

    def __getitem__(self, key):          # row[0] 또는 row["name"] 양쪽 지원
        if isinstance(key, int):
            return self._vals[key]
        return self._vals[self._cols.index(key)]

    def __iter__(self):                  # dict(커서.fetchall())처럼 쌍으로 묶는 기존 용법 지원
        return iter(self._vals)

    def __len__(self):
        return len(self._vals)

    def keys(self):                      # sqlite3.Row.keys()와 같은 이름
        return self._cols


def _pg_rows(cursor):
    """psycopg의 행 팩토리 — 결과 각 줄을 PgRow로 감싼다."""
    cols = [d.name for d in cursor.description] if cursor.description else []

    def make(vals):
        return PgRow(cols, vals)
    return make


class PgConn:
    """psycopg 연결을 sqlite3.Connection처럼 보이게 하는 얇은 껍데기.
    두 DB의 차이 나는 부분만 여기서 흡수한다:
    ①자리표 문자(SQLite ? → Postgres %s) ②executemany가 커서에만 있는 것."""

    def __init__(self, con):
        self._con = con

    def execute(self, sql, params=None):
        # params가 없으면 psycopg에 아예 안 넘긴다 — 빈 튜플이라도 넘기면 psycopg가 문장 속 %를
        # 전부 자리표로 해석해서, LIKE 'kakao_%' 같은 순수 SQL의 %에서 죽는다
        # (카톡 해제 500의 원인 — 2026-08-11 실측. SQLite에선 %가 특별하지 않아 잠복해 있었다).
        sql = sql.replace("?", "%s")
        if params is None or params == ():
            return self._con.execute(sql)
        return self._con.execute(sql, params)

    def executemany(self, sql, rows):
        with self._con.cursor() as cur:
            cur.executemany(sql.replace("?", "%s"), rows)

    def commit(self):
        self._con.commit()

    def close(self):
        self._con.close()


def db():
    """DB 연결을 연다 — 환경변수에 따라 Postgres(배포) 또는 SQLite(로컬).
    어느 쪽이든 결과 줄은 번호·열 이름 양쪽으로 꺼낼 수 있다."""
    if IS_PG:
        return PgConn(psycopg.connect(DATABASE_URL, row_factory=_pg_rows))
    con = sqlite3.connect(DB_PATH, timeout=15)   # 잠금 대기 넉넉히 — 시드 직후 등 일시 경합에 죽지 않게
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")   # 없는 거래처·담당자를 가리키는 데이터가 못 들어오게
    return con


def setting_get(con, key: str):
    """설정 값 하나 읽기 (settings 표). 없으면 None."""
    row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def setting_set(con, key: str, value: str) -> None:
    """설정 값 저장 — 있으면 덮어쓴다. (UPSERT 문법은 SQLite·Postgres 동일)"""
    con.execute("""INSERT INTO settings (key, value) VALUES (?,?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (key, value))


def insert_id(con, sql: str, params) -> int:
    """INSERT 하고 새 줄의 id를 돌려준다.
    SQLite는 lastrowid로 주지만 Postgres에는 그 기능이 없어 RETURNING id로 받는다."""
    if IS_PG:
        return con.execute(sql + " RETURNING id", params).fetchone()[0]
    return con.execute(sql, params).lastrowid


def sql_avg_hours(done: str, created: str) -> str:
    """「완료까지 걸린 평균 시간(시간 단위)」 SQL 조각 — 날짜 계산 함수가 DB마다 달라 갈라준다.
    시각을 TEXT('YYYY-MM-DD HH:MM')로 저장하므로, SQLite는 julianday(날짜→일수),
    Postgres는 ::timestamp로 바꾼 뒤 EPOCH(초 차이)로 계산한다."""
    if IS_PG:
        return (f"ROUND((AVG(EXTRACT(EPOCH FROM ({done}::timestamp - {created}::timestamp))"
                f" / 3600.0))::numeric, 1)")
    return f"ROUND(AVG((julianday({done}) - julianday({created})) * 24), 1)"


def save_photos(photos: list[UploadFile] | None) -> list[str]:
    r"""올라온 사진들을 uploads\ 에 저장하고 파일명 목록을 돌려준다.
    왜 사진인가: "얼룩이 있대"(말)와 "여기 이 얼룩"(사진)의 차이 —
    어떤 시트의 어느 부분에 오점이 반복되는지를 직원·기사가 눈으로 알게 하려고 (2026-08-07 인각님 추가)."""
    names = []
    for photo in photos or []:
        if not photo.filename:
            continue
        ext = Path(photo.filename).suffix.lower()
        if ext not in {".jpg", ".jpeg", ".png", ".webp"}:
            continue                   # 사진 파일만 받는다 (그 외는 조용히 무시 — v1)
        name = datetime.now(KST).strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6] + ext
        (UPLOAD_DIR / name).write_bytes(photo.file.read())
        names.append(name)
    return names


def attach_photos(con, action_id: int, photos: list[UploadFile] | None) -> None:
    """저장된 사진들을 조치 한 건에 매단다 (photos 표에 한 장당 한 줄)."""
    for name in save_photos(photos):
        con.execute("INSERT INTO photos (action_id, filename) VALUES (?,?)", (action_id, name))


# ── 카톡 알림 (v2, 2026-08-10) ───────────────────────────────────────
# SMS 대신 카카오톡 「나에게 보내기」 — 무료 API의 유일한 발송 범위가 본인 계정이라(친구 발송은 심사),
# 모든 알림은 연동한 관리자(사장) 본인의 카톡으로 간다. "새 클레임이 오면 카톡이 울린다"의 증명.
# 토큰 보관은 settings 표(영속 DB) — 카카오 액세스 토큰은 수 시간짜리라 리프레시 토큰으로 그때그때 갱신한다.
KAKAO_REST_KEY = os.environ.get("KAKAO_REST_KEY", "")          # 카카오 디벨로퍼스 앱의 REST API 키
KAKAO_CLIENT_SECRET = os.environ.get("KAKAO_CLIENT_SECRET", "")  # 앱의 [보안] Client Secret이 「사용함」이면 필수
SERVICE_URL = os.environ.get("RENDER_EXTERNAL_URL", "")        # Render가 자동으로 넣어주는 자기 주소


def _kakao_redirect_uri(request: Request) -> str:
    """카카오 동의 뒤 돌아올 주소 — 등록된 Redirect URI와 글자까지 같아야 해서 한 곳에서 만든다.
    배포에선 Render가 주는 공개 주소(https), 로컬에선 요청받은 주소 그대로."""
    base = SERVICE_URL or str(request.base_url).rstrip("/")
    return base + "/kakao/callback"


def _kakao_token_request(fields: dict) -> dict:
    """카카오 토큰 서버(kauth) 호출 — 최초 발급과 갱신이 같은 주소를 쓴다.
    실패 시 카카오는 사유를 JSON(에러 코드 KOE___)으로 준다 — 500으로 죽지 말고
    그 JSON을 그대로 돌려줘서 화면에서 원인을 읽을 수 있게 한다."""
    if KAKAO_CLIENT_SECRET:
        fields = dict(fields, client_secret=KAKAO_CLIENT_SECRET)
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request("https://kauth.kakao.com/oauth/token", data=data)
    try:
        return json.loads(urllib.request.urlopen(req, timeout=10).read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return {"error": "http_error", "error_description": f"토큰 서버 응답 {e.code}"}


def kakao_access_token():
    """보낼 때마다 유효한 액세스 토큰을 준비한다. 미연동이면 None.
    남은 시간이 1분 미만이면 리프레시 토큰으로 갱신하고, 리프레시 토큰이 회전되어 새로 오면
    (만료 1달 전부터 온다) 그것도 DB에 다시 저장 — 서버 재시작과 무관하게 연동이 유지된다."""
    if not KAKAO_REST_KEY:
        return None
    con = db()
    try:
        access = setting_get(con, "kakao_access_token")
        expires = setting_get(con, "kakao_access_expires")   # epoch 초 (글자로 저장)
        if access and expires and time.time() < float(expires) - 60:
            return access
        refresh = setting_get(con, "kakao_refresh_token")
        if not refresh:
            return None                                       # 아직 연동 안 함 (/kakao/setup 전)
        tok = _kakao_token_request({"grant_type": "refresh_token",
                                    "client_id": KAKAO_REST_KEY, "refresh_token": refresh})
        if "access_token" not in tok:
            # 갱신 실패(카카오 쪽에서 연결이 끊겼거나 리프레시 토큰 만료) — 죽지 말고 미연동 취급.
            # 죽은 토큰을 쥔 채 KeyError로 500 나던 것의 수리(2026-08-11, 해제 반쪽 성공 사고 때 발견).
            print("카카오 토큰 갱신 실패(미연동 취급):", tok.get("error"), tok.get("error_description"))
            return None
        setting_set(con, "kakao_access_token", tok["access_token"])
        setting_set(con, "kakao_access_expires", str(time.time() + tok.get("expires_in", 21600)))
        if tok.get("refresh_token"):
            setting_set(con, "kakao_refresh_token", tok["refresh_token"])
        con.commit()
        return tok["access_token"]
    finally:
        con.close()


def kakao_send_to_me(text: str, url: str | None = None) -> bool:
    """카카오톡 「나에게 보내기」 — 기본 텍스트 템플릿. 보냈으면 True, 미연동이면 False."""
    token = kakao_access_token()
    if not token:
        return False
    link = url or SERVICE_URL or "https://laundry-claim.onrender.com"
    template = {"object_type": "text", "text": text,
                "link": {"web_url": link, "mobile_web_url": link}, "button_title": "열어보기"}
    data = urllib.parse.urlencode(
        {"template_object": json.dumps(template, ensure_ascii=False)}).encode()
    req = urllib.request.Request("https://kapi.kakao.com/v2/api/talk/memo/default/send", data=data)
    req.add_header("Authorization", f"Bearer {token}")
    urllib.request.urlopen(req, timeout=10)
    return True


def notify(target: str, message: str, ticket_id: int | None = None) -> None:
    """알림 발송 — v2에서 카카오톡 「나에게 보내기」로 구현됨.
    실패·미연동이어도 서비스는 계속 돈다 — 알림은 보조 수단이고,
    원 알림 축은 여전히 ①공장 모니터 대시보드 ②담당자의 「내 클레임」 화면이다."""
    try:
        link = (SERVICE_URL + f"/c/{ticket_id}") if (SERVICE_URL and ticket_id) else None
        kakao_send_to_me(f"[세탁 클레임] {message}\n담당: {target}", link)
    except Exception as e:                 # 알림이 죽어도 접수는 살아야 한다
        print("카톡 알림 실패(무시하고 계속):", e)


def seed_demo(con) -> None:
    """시연용 예시 데이터를 넣는다. 최초 부팅(빈 DB)과 데모 리셋 두 곳에서 부른다."""
    con.executemany("INSERT INTO factories (id, name, note) VALUES (?,?,?)", [
        (1, "제1공장 (창동)", "공장장 1 + 인력 16 + 기사 2 · 담당 8개 업체 · 월 매출 약 1.6억 가정(업체당 약 2천만)"),
        (2, "제2공장 (의정부)", "공장장 1 + 인력 4 + 기사 1 · 담당 2개 업체"),
    ])
    # 하루 물량 추정 공식: 객실수 × 가동률 70% × 객실당 리넨 약 4kg
    # (요양병원 = 침상 × 3kg, 웨딩홀 = 행사 기준이라 객실수 0)
    con.executemany("""INSERT INTO clients
        (factory_id, name, biz_type, rooms, daily_kg, note) VALUES (?,?,?,?,?,?)""", [
        (1, "그랜드한강호텔",        "관광호텔",     350, 980, "이불류는 항상 개별 포장"),
        (1, "수유 리버사이드호텔",   "관광호텔",     300, 840, ""),
        (1, "의정부 엠스테이션호텔", "비즈니스호텔", 200, 560, "고객 유실물 발견 시 즉시 보고"),
        (1, "창동 호텔더블유",       "비즈니스호텔", 180, 500, ""),
        (1, "스테이노원",            "비즈니스호텔", 150, 420, ""),
        (1, "강북성심요양병원",      "요양병원",     300, 900, "환자복·시트 분리 수거, 감염성 세탁물 별도 처리"),
        (1, "도봉 베뉴모텔",         "모텔",          60, 170, ""),
        (1, "미아 클라우드모텔",     "모텔",          45, 130, ""),
        (2, "의정부 그랜드컨벤션",   "웨딩홀·연회",    0, 600, "행사 일정 따라 물량 변동 큼 — 주말 집중"),
        (2, "포천 힐스파리조트",     "온천리조트",   120, 340, "주말 물량 2배"),
    ])
    # 인력: 제1공장 공장장1+16+기사4, 제2공장 공장장1+4+기사1
    # 공정은 세탁공장 실제 흐름(세탁→건조→다림→포장→검수)에서, 입사일은 돌려가며 배정
    duties = ["세탁", "건조", "다림(롤러)", "포장", "검수"]
    hires = ["2019-03", "2020-11", "2021-06", "2022-02", "2022-09", "2023-04", "2024-01", "2025-05"]
    rows = [
        (None, "사장", "owner", "본사 — 두 공장 전체 총괄", "2018-01"),   # factory_id 없음 = 전체를 본다
        (1, "강만석", "manager", "제1공장 총괄 (생산·품질 책임)", "2018-05"),
        (2, "윤정례", "manager", "제2공장 총괄", "2021-01"),
    ]
    f1_workers = ["김세탁", "이다림", "박정리", "최민수", "정호영", "한가람", "오세훈", "서지우",
                  "남기웅", "문채원", "임태호", "조성민", "배수지", "신동엽", "권나라", "황보라"]
    rows += [(1, n, "factory", duties[i % 5] + " 담당", hires[i % 8]) for i, n in enumerate(f1_workers)]
    rows += [(2, n, "factory", duties[i % 5] + " 담당", hires[(i + 3) % 8])
             for i, n in enumerate(["장미란", "전상국", "고아라", "유병재"])]
    rows += [
        (1, "박기사", "driver", "1호차 — 창동·도봉 방면", "2020-04"),
        (1, "최배송", "driver", "2호차 — 수유·미아 방면", "2022-08"),
        (1, "정노선", "driver", "3호차 — 의정부 방면", "2023-10"),
        (1, "한기동", "driver", "4호차 — 대형 호텔 전담", "2024-06"),
        (2, "나운전", "driver", "의정부·포천 방면", "2022-03"),
    ]
    for i, (fid, name, role, duty, hired) in enumerate(rows):
        con.execute("INSERT INTO staff (factory_id, name, role, duty, phone, hired_at) VALUES (?,?,?,?,?,?)",
                    (fid, name, role, duty, f"010-20{i:02d}-{3000 + i * 7:04d}", hired))  # 번호는 전부 가짜

    # ── 품목 사전 + 거래처별 취급 품목 ──────────────────────
    # 품목 이름은 현장 용어대로 (대타올·중타올·발매트 — 2026-08-07 인각님 용어)
    item_names = ["시트", "이불커버", "베개피", "대타올", "중타올", "발매트",
                  "가운", "침대패드", "환자복", "담요", "테이블보", "냅킨"]
    con.executemany("INSERT INTO items (name) VALUES (?)", [(n,) for n in item_names])
    item_id = {n: i + 1 for i, n in enumerate(item_names)}

    # 객실 하나(투숙 기준)가 하루에 내놓는 표준 세트 — 인각님 현장 실측 기준
    # (시트 1 · 커버 1 · 베개피 2~4 · 대타올 1~2 · 중타올 3~4 · 발매트 1. 2026-08-07 정정 —
    #  Claude 초기 추정 "시트 2장"이 틀렸던 것)
    HOTEL_SET = {"시트": 1, "이불커버": 1, "베개피": 3, "대타올": 2, "중타올": 3,
                 "발매트": 1, "침대패드": 0.2}
    profiles = {
        "그랜드한강호텔":        dict(HOTEL_SET, **{"가운": 2}),   # 관광호텔 — 가운까지 풀 세트
        "수유 리버사이드호텔":   dict(HOTEL_SET, **{"가운": 2}),
        "의정부 엠스테이션호텔": dict(HOTEL_SET),                  # 비즈니스 — 가운 없음
        "창동 호텔더블유":       dict(HOTEL_SET),
        "스테이노원":            {k: v for k, v in HOTEL_SET.items() if k != "발매트"},  # 발매트도 없음
        "도봉 베뉴모텔":         {"시트": 1, "이불커버": 1, "베개피": 2, "대타올": 1, "중타올": 3},
        "미아 클라우드모텔":     {"시트": 1, "이불커버": 1, "베개피": 2, "대타올": 1, "중타올": 3},
        "포천 힐스파리조트":     dict(HOTEL_SET, **{"가운": 2}),
    }
    for cname, prof in profiles.items():
        crow = con.execute("SELECT id, rooms FROM clients WHERE name=?", (cname,)).fetchone()
        occupied = int(crow["rooms"] * 0.7)          # 가동률 70% 가정
        for iname, per_room in prof.items():
            con.execute("INSERT INTO client_items VALUES (?,?,?)",
                        (crow["id"], item_id[iname], int(occupied * per_room)))
    # 표준 세트가 안 맞는 두 곳은 손으로 (요양병원 = 환자복 중심, 웨딩홀 = 테이블 리넨 중심)
    py_id = con.execute("SELECT id FROM clients WHERE name='강북성심요양병원'").fetchone()["id"]
    for iname, qty in {"환자복": 210, "시트": 100, "베개피": 100, "담요": 30, "중타올": 150}.items():
        con.execute("INSERT INTO client_items VALUES (?,?,?)", (py_id, item_id[iname], qty))
    wd_id = con.execute("SELECT id FROM clients WHERE name='의정부 그랜드컨벤션'").fetchone()["id"]
    for iname, qty in {"테이블보": 120, "냅킨": 700, "중타올": 60}.items():
        con.execute("INSERT INTO client_items VALUES (?,?,?)", (wd_id, item_id[iname], qty))

    def sid(name):
        """이름으로 담당자 id 찾기 (예시 티켓 배정용)."""
        return con.execute("SELECT id FROM staff WHERE name=?", (name,)).fetchone()[0]

    def add_ticket(client, type_, sev, content, channel, assignee, created, acts, status, done=None):
        """예시 티켓 한 건 + 조치 일지 + 사진.
        acts = [(누가, 종류, 내용, 시각, [사진 파일명들])]. 사진은 make_demo_photos.py 가 만든 데모 이미지."""
        tid = insert_id(con, """INSERT INTO complaints
            (client_id, type, severity, content, channel, assignee_id, status, created_at, done_at)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (client, type_, sev, content, channel, sid(assignee), status, created, done))
        for actor, kind, text, at, photo_files in acts:
            aid = insert_id(con, """INSERT INTO actions (complaint_id, actor, kind, content, created_at)
                                    VALUES (?,?,?,?,?)""", (tid, actor, kind, text, at))
            for ph in photo_files:
                con.execute("INSERT INTO photos (action_id, filename) VALUES (?,?)", (aid, ph))

    # ── 예시 티켓 — 두 공장 모두, 여러 날짜에 걸쳐 (완료 이력이 있어야 평균 처리 시간이 산다) ──
    today = now()[:10]
    add_ticket(1, "delivery", "urgent", "어제 납품분에 다른 업체 수건이 섞여 왔음. 회수 요청.",
               "전화", "박기사", f"{today} 08:40", [
        ("사장", "register", "그랜드한강호텔 지배인 전화 접수", f"{today} 08:40", ["demo_towels_mixed.png"]),
        ("사장", "instruct", "오늘 수거 때 섞인 수건 회수해 올 것", f"{today} 08:42", []),
    ], "new")
    add_ticket(6, "quality", "normal", "환자복 소매 얼룩이 안 빠진 채 납품됨 (3벌)",
               "문자", "김세탁", "2026-08-06 14:10", [
        ("사장", "register", "요양병원 문자 접수, 사진 받음", "2026-08-06 14:10", ["demo_sleeve_stain.png"]),
        ("김세탁", "work", "재세탁 진행. 녹 계열 얼룩이라 전용 처리 필요", "2026-08-06 16:30", []),
    ], "working")
    add_ticket(7, "quality", "normal", "수건 5장이 누렇게 변색된 채 왔다고 교환 요청",
               "전화", "이다림", "2026-08-05 10:20", [
        ("사장", "register", "베뉴모텔 사장님 전화", "2026-08-05 10:20", ["demo_towel_yellow.png"]),
        ("이다림", "ack", "지시 확인", "2026-08-05 10:55", []),
        ("이다림", "work", "재세탁 후 검수 — 변색 심한 3장은 폐기, 새 수건으로 교체", "2026-08-05 14:20", []),
        ("이다림", "done", "교체분 당일 배송 완료", "2026-08-05 15:40", []),
    ], "done", "2026-08-05 15:40")
    add_ticket(9, "quality", "urgent", "토요일 행사분 테이블보에 와인 얼룩 12장이 그대로 납품됨",
               "전화", "장미란", f"{today} 09:15", [
        ("사장", "register", "웨딩홀 매니저 전화 — 다음 행사가 금요일이라 급함", f"{today} 09:15", ["demo_tablecloth_wine.png"]),
        ("장미란", "ack", "지시 확인", f"{today} 09:40", []),
        ("장미란", "work", "전량 회수해 얼룩 전용 재세탁 돌리는 중", f"{today} 11:05", []),
    ], "working")
    add_ticket(10, "delivery", "normal", "가운 40장 수량 부족 — 주말 투숙 앞두고 보충 요청",
               "문자", "나운전", f"{today} 10:30", [
        ("사장", "register", "리조트 프런트 문자", f"{today} 10:30", []),
        ("사장", "instruct", "내일 오전 배송 때 가운 40장 추가 적재", f"{today} 10:33", []),
        ("나운전", "ack", "지시 확인", f"{today} 12:02", []),
    ], "acked")
    add_ticket(10, "quality", "normal", "스파 타월에서 꿉꿉한 냄새가 난다는 고객 불만",
               "전화", "윤정례", "2026-08-04 09:00", [
        ("사장", "register", "리조트 지배인 전화", "2026-08-04 09:00", ["demo_towel_mold.png"]),
        ("윤정례", "work", "건조 공정 점검 — 건조기 2호기 온도 저하 발견", "2026-08-04 13:30", []),
        ("윤정례", "done", "재세탁·완전 건조 후 재납품. 건조기 수리 완료", "2026-08-04 17:10", []),
    ], "done", "2026-08-04 17:10")
    add_ticket(5, "etc", "normal", "수거 방문을 오전 7시 이전으로 바꿔달라는 요청",
               "전화", "최배송", "2026-08-06 09:50", [
        ("사장", "register", "스테이노원 프런트 전화", "2026-08-06 09:50", []),
        ("사장", "instruct", "2호차 노선 순서 조정 검토", "2026-08-06 09:55", []),
    ], "new")
    # ── 추가분: 여러 인력·여러 날짜에 걸친 이력 (인력별 배정 화면이 의미 있으려면 골고루 필요) ──
    add_ticket(2, "quality", "normal", "가운 허리끈 6개가 세탁 후 사라졌다는 항의",
               "전화", "문채원", "2026-08-02 11:20", [
        ("사장", "register", "리버사이드 하우스키핑 전화", "2026-08-02 11:20", []),
        ("문채원", "work", "포장 라인 점검 — 끈 분리 세탁분이 별도 망에 있었음", "2026-08-02 14:00", []),
        ("문채원", "done", "6개 전량 찾아 당일 재납품", "2026-08-02 16:30", []),
    ], "done", "2026-08-02 16:30")
    add_ticket(3, "delivery", "normal", "납품이 이틀 연속 오전 11시를 넘겼다는 항의",
               "문자", "정노선", "2026-08-03 09:10", [
        ("사장", "register", "엠스테이션 지배인 문자", "2026-08-03 09:10", []),
        ("정노선", "ack", "지시 확인", "2026-08-03 09:30", []),
        ("정노선", "done", "3호차 출발 순서를 바꿔 9시 30분 납품으로 조정", "2026-08-03 13:00", []),
    ], "done", "2026-08-03 13:00")
    add_ticket(4, "quality", "normal", "시트 구김이 심해 다시 다려달라는 요청 (20장)",
               "전화", "한가람", "2026-08-06 15:40", [
        ("사장", "register", "호텔더블유 프런트 전화", "2026-08-06 15:40", []),
        ("한가람", "ack", "지시 확인", "2026-08-06 16:00", []),
        ("한가람", "work", "롤러 온도 재설정 후 재다림 중", "2026-08-06 17:10", []),
    ], "working")
    add_ticket(8, "delivery", "urgent", "오늘 수거를 안 왔다는 연락 — 세탁물이 쌓여 있음",
               "전화", "최배송", "2026-08-01 17:30", [
        ("사장", "register", "클라우드모텔 사장님 전화", "2026-08-01 17:30", []),
        ("최배송", "ack", "지시 확인 — 노선 착오였음", "2026-08-01 17:45", []),
        ("최배송", "done", "당일 저녁 수거 완료, 다음날 우선 납품", "2026-08-01 19:20", []),
    ], "done", "2026-08-01 19:20")
    add_ticket(5, "quality", "normal", "베개피 30장이 수량 부족으로 납품됨",
               "문자", "박정리", "2026-07-31 10:00", [
        ("사장", "register", "스테이노원 문자", "2026-07-31 10:00", []),
        ("박정리", "work", "포장 대수 대조 — 다른 호텔 묶음에 섞여 나간 것 확인", "2026-07-31 11:30", []),
        ("박정리", "done", "30장 회수·재납품, 포장 검수 절차에 수량 체크 추가", "2026-07-31 15:00", []),
    ], "done", "2026-07-31 15:00")
    add_ticket(1, "etc", "normal", "빈 세탁망 40개를 다음 수거 때 돌려달라는 요청",
               "전화", "한기동", f"{today} 11:50", [
        ("사장", "register", "그랜드한강 하우스키핑 전화", f"{today} 11:50", []),
        ("사장", "instruct", "4호차 내일 적재분에 세탁망 40개 포함", f"{today} 11:52", []),
    ], "new")
    add_ticket(9, "delivery", "urgent", "금요일 행사가 목요일로 앞당겨짐 — 납품 하루 앞당겨달라",
               "전화", "나운전", "2026-08-06 16:20", [
        ("사장", "register", "웨딩홀 매니저 전화", "2026-08-06 16:20", []),
        ("나운전", "ack", "지시 확인", "2026-08-06 16:40", []),
        ("나운전", "work", "목요일 오전 배송으로 일정 재편성 중", "2026-08-06 17:30", []),
    ], "working")
    add_ticket(10, "quality", "normal", "침대패드 4장에 누런 얼룩 — 교체 요청",
               "문자", "고아라", "2026-08-05 13:10", [
        ("사장", "register", "리조트 하우스키핑 문자", "2026-08-05 13:10", ["demo_towel_yellow.png"]),
        ("고아라", "work", "재세탁 시험 — 오래된 얼룩이라 표백 처리", "2026-08-05 15:00", []),
        ("고아라", "done", "2장 복원, 2장은 폐기 후 신품 교체", "2026-08-05 18:20", []),
    ], "done", "2026-08-05 18:20")
    add_ticket(9, "quality", "normal", "냅킨 100장 다림 상태 불량 — 행사용으로 못 쓴다는 항의",
               "전화", "전상국", "2026-08-02 10:40", [
        ("사장", "register", "웨딩홀 매니저 전화", "2026-08-02 10:40", []),
        ("전상국", "work", "재다림 진행", "2026-08-02 13:00", []),
        ("전상국", "done", "전량 재다림 후 당일 납품", "2026-08-02 16:00", []),
    ], "done", "2026-08-02 16:00")


def init_db() -> None:
    """표를 만들고, 비어 있으면 시연용 예시 데이터를 넣는다. (SQLite·Postgres 공용)"""
    # id 자동 번호 방식이 다르다: SQLite는 INTEGER PRIMARY KEY면 자동,
    # Postgres는 IDENTITY 선언이 있어야 한다 (BY DEFAULT = 예시 데이터의 명시 id도 허용)
    auto_id = ("INTEGER PRIMARY KEY" if not IS_PG
               else "INTEGER PRIMARY KEY GENERATED BY DEFAULT AS IDENTITY")
    ddl = f"""
    CREATE TABLE IF NOT EXISTS factories (    -- 공장 — 거래처와 인력이 여기 소속된다
        id   {auto_id},
        name TEXT NOT NULL,
        note TEXT DEFAULT ''                  -- 인력 구성 같은 요약
    );
    CREATE TABLE IF NOT EXISTS clients (      -- 거래처
        id         {auto_id},
        factory_id INTEGER REFERENCES factories(id),   -- 어느 공장이 처리하나
        name       TEXT NOT NULL,
        biz_type   TEXT DEFAULT '',           -- 업태 (관광호텔·모텔·요양병원…)
        rooms      INTEGER DEFAULT 0,         -- 객실(침상) 수 — 물량 추정의 근거
        daily_kg   INTEGER DEFAULT 0,         -- 하루 물량 추정(kg)
        note       TEXT DEFAULT ''            -- 상시 특이사항 ("이불은 항상 개별 포장" 같은 것)
    );
    CREATE TABLE IF NOT EXISTS staff (        -- 담당자 (공장장·공장 인력·배송기사를 한 표에, role로 구분)
        id         {auto_id},
        factory_id INTEGER REFERENCES factories(id),
        name       TEXT NOT NULL,
        role       TEXT NOT NULL CHECK (role IN ('owner','manager','factory','driver')),
        duty       TEXT DEFAULT '',           -- 담당 업무: 인력=공정(세탁·다림…), 기사=노선(1호차 창동 방면)
        phone      TEXT DEFAULT '',           -- 연락처 (전부 가짜 — v2 문자 발송이 갈 자리)
        hired_at   TEXT DEFAULT ''            -- 입사 연월
    );
    CREATE TABLE IF NOT EXISTS items (        -- 품목 사전 (시트·베개피·수건…)
        id   {auto_id},
        name TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS client_items ( -- 거래처별 취급 품목 — 호텔마다 구성이 다르다
        client_id INTEGER NOT NULL REFERENCES clients(id),   -- (모텔엔 가운·발수건이 없는 식)
        item_id   INTEGER NOT NULL REFERENCES items(id),
        daily_qty INTEGER DEFAULT 0,          -- 하루 수량 추정(장)
        PRIMARY KEY (client_id, item_id)
    );
    CREATE TABLE IF NOT EXISTS complaints (   -- 컴플레인 티켓 (이 시스템의 중심 개체)
        id          {auto_id},
        client_id   INTEGER NOT NULL REFERENCES clients(id),
        type        TEXT NOT NULL CHECK (type IN ('quality','delivery','etc')),
        severity    TEXT NOT NULL DEFAULT 'normal' CHECK (severity IN ('normal','urgent')),
        content     TEXT NOT NULL,            -- 무슨 컴플레인인가 (전화·문자로 들은 그대로)
        channel     TEXT DEFAULT '전화',       -- 어디로 들어왔나 (전화/문자/방문)
        assignee_id INTEGER REFERENCES staff(id),   -- 누가 맡나
        status      TEXT NOT NULL DEFAULT 'new'
                    CHECK (status IN ('new','acked','working','done')),
        created_at  TEXT NOT NULL,
        done_at     TEXT                      -- 완료 시각 (처리 시간 계산용)
    );
    CREATE TABLE IF NOT EXISTS actions (      -- 조치 일지 — 티켓의 타임라인 (이력 보존형 장부)
        id           {auto_id},
        complaint_id INTEGER NOT NULL REFERENCES complaints(id),
        actor        TEXT NOT NULL,           -- 누가 (이름 글자 — v1은 로그인이 없으므로)
        kind         TEXT NOT NULL CHECK (kind IN ('register','instruct','ack','work','done','note','move')),
        content      TEXT DEFAULT '',
        created_at   TEXT NOT NULL,
        edited_at    TEXT,                    -- 마지막 수정 시각 (본인 글만 고칠 수 있다 — 원문은 action_edits에)
        deleted_at   TEXT                     -- 삭제 시각 — 줄을 지우지 않고 취소선으로 남긴다 (장부 흔적)
    );
    CREATE TABLE IF NOT EXISTS action_edits ( -- 조치 수정 이력 — 고치기 전 내용 보관
        id          {auto_id},                -- (2026-08-12 인각님 결정: 본인 글 수정·삭제는 허용하되
        action_id   INTEGER NOT NULL REFERENCES actions(id),   --  원문이 증거로 남아야 한다)
        old_content TEXT NOT NULL,            -- 수정 직전 내용
        edited_at   TEXT NOT NULL             -- 언제 고쳤나
    );
    CREATE TABLE IF NOT EXISTS photos (       -- 첨부 사진 — 조치 하나에 여러 장(1:N)이라 표를 따로 둔다
        id        {auto_id},                  -- (한 칸에 파일명 여러 개를 욱여넣으면 정규화 위반)
        action_id INTEGER NOT NULL REFERENCES actions(id),
        filename  TEXT NOT NULL               -- uploads\\ 안의 파일명
    );
    CREATE TABLE IF NOT EXISTS settings (     -- 앱 설정 (카카오 토큰 등) — 재시작에도 남아야 해서 DB에
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS chats (            -- 상담 대화방 — 한 사람이 주제별로 여러 개
        id         {auto_id},
        staff_id   INTEGER NOT NULL REFERENCES staff(id),   -- 방 주인 (로그인 사용자)
        title      TEXT NOT NULL,                 -- 방 제목 = 첫 질문 앞 30자
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS chat_msgs (        -- 대화 내용 — 말풍선 하나가 한 줄 (수정 없음, 쌓기만)
        id         {auto_id},
        chat_id    INTEGER NOT NULL REFERENCES chats(id),
        role       TEXT NOT NULL CHECK (role IN ('user','bot')),
        content    TEXT NOT NULL,
        sources    TEXT DEFAULT '',               -- 근거 목록 JSON 문자열 (bot 답에만)
        ok         INTEGER NOT NULL DEFAULT 1,    -- 0 = 오류·연결 실패 안내였음
        created_at TEXT NOT NULL
    );
    """
    con = db()
    if IS_PG:
        # psycopg는 여러 문장을 한 번에 못 보내므로 문장(;) 단위로 나눠 실행
        for stmt in ddl.split(";"):
            if stmt.strip():
                con.execute(stmt)
        # 이관(2라운드): 조치 종류에 'move' 추가 — 이미 만들어진 표의 CHECK 제약을 갈아끼운다
        # (CREATE IF NOT EXISTS는 기존 표를 안 건드리므로 제약만 따로. 매 부팅 반복해도 결과는 같다)
        con.execute("ALTER TABLE actions DROP CONSTRAINT IF EXISTS actions_kind_check")
        con.execute("ALTER TABLE actions ADD CONSTRAINT actions_kind_check CHECK "
                    "(kind IN ('register','instruct','ack','work','done','note','move'))")
        # 세탁 상담 챗 구조 변경(질문·답 한 줄 ask_log → 대화방 여러 개 chats/chat_msgs):
        # 시험 기록 몇 줄뿐이라 옮기지 않고 정리한다. SQLite 쪽은 로컬 파일이라 매번 새로
        # 만들어지므로(위 CREATE IF NOT EXISTS로 이미 새 표만 생김) 별도 조치가 필요 없다.
        con.execute("DROP TABLE IF EXISTS ask_log")
        # 이관(2026-08-12): 조치 수정·삭제 칸 — 이미 만들어진 표에 칸만 보탠다 (매 부팅 반복 무해)
        con.execute("ALTER TABLE actions ADD COLUMN IF NOT EXISTS edited_at TEXT")
        con.execute("ALTER TABLE actions ADD COLUMN IF NOT EXISTS deleted_at TEXT")
    else:
        con.executescript(ddl)
        # 이관: 예전 판은 actions.photo 한 칸에 사진 하나였다 → photos 표로 옮기고 칸을 없앤다
        # (로컬 SQLite 파일에만 있을 수 있는 과거 흔적이라 SQLite에서만 확인)
        old_cols = [row[1] for row in con.execute("PRAGMA table_info(actions)")]
        if "photo" in old_cols:
            con.execute("""INSERT INTO photos (action_id, filename)
                           SELECT id, photo FROM actions WHERE photo != ''""")
            con.execute("ALTER TABLE actions DROP COLUMN photo")
        # 이관(2026-08-12): 조치 수정·삭제 칸 (기존 로컬 파일에만 필요 — 새 파일은 위 DDL에 이미 있다)
        for col in ("edited_at", "deleted_at"):
            if col not in old_cols:
                con.execute(f"ALTER TABLE actions ADD COLUMN {col} TEXT")
    # 비어 있을 때만 예시 데이터 (시연용 — 실제 도입 시 지우면 됨)
    # ⚠️ 업체 이름은 전부 가상이다. 실존 호텔·모텔의 「유형과 규모」만 본떴다 —
    #    실명에 지어낸 컴플레인을 붙여 공개 배포하면 그 업체에 대한 허위 기록이 되기 때문.
    if con.execute("SELECT COUNT(*) FROM clients").fetchone()[0] == 0:
        seed_demo(con)
    con.commit()
    con.close()


init_db()


def ensure_demo_photos():
    """배포 서버는 재시작 때 디스크가 초기화된다 — 데모 사진이 없으면 켜질 때 만들어 둔다.
    (없으면 예시 클레임의 사진이 깨진 이미지로 보이므로.)"""
    if not list(UPLOAD_DIR.glob("demo_*.png")):
        try:
            import make_demo_photos  # noqa: F401 — 불러오는 것 자체가 생성 실행
        except Exception as e:                     # 사진은 장식이라, 실패해도 서비스는 켠다
            print("데모 사진 생성 실패(치명적 아님):", e)


ensure_demo_photos()


def require_user(request: Request):
    """로그인한 담당자를 쿠키에서 찾는다. 없으면 None — 각 화면은 None이면 /login으로 보낸다.
    ⚠️ v1은 간이 로그인이다: 서명 없는 쿠키에 담당자 번호만 담고 비밀번호가 없다.
    목적이 「누가 무엇을 보는가」(인가)의 시연이기 때문 — 본인 증명(인증)은 공개 배포 때 판단."""
    uid = request.cookies.get("uid", "")
    if not uid.isdigit():
        return None
    con = db()
    row = con.execute("SELECT * FROM staff WHERE id=?", (int(uid),)).fetchone()
    con.close()
    return row


def scope_of(user) -> int | None:
    """이 사용자가 볼 수 있는 공장 범위. None = 전체(본사), 숫자 = 그 공장만."""
    return None if user["role"] == "owner" else user["factory_id"]


def recent_tickets(user):
    """사이드바 「최근 업데이트」용 — 최근 미완료 티켓 3건, 보는 사람의 공장 범위 안에서만."""
    fid = scope_of(user)
    con = db()
    rows = con.execute(TICKET_SELECT + " WHERE c.status != 'done'"
                       + (" AND cl.factory_id=?" if fid else "")
                       + " ORDER BY c.created_at DESC LIMIT 3",
                       (fid,) if fid else ()).fetchall()
    con.close()
    return rows


# ── 공통: 티켓 목록을 화면용으로 읽는 SQL (거래처·담당자 이름까지 JOIN) ──
TICKET_SELECT = """
SELECT c.*, cl.name AS client_name, s.name AS assignee_name, s.role AS assignee_role,
       f.name AS factory_name
FROM complaints c
JOIN clients cl ON cl.id = c.client_id
LEFT JOIN factories f ON f.id = cl.factory_id
LEFT JOIN staff s ON s.id = c.assignee_id
"""


# ── 세탁 상담 챗 (2026-08-10, HANDOFF-ask-chat.md) ─────────────────────
# 노트북의 상담 모델(Ollama+Qwen3, 사서 방식)이 두뇌, 이 서버는 얼굴 — 채팅 UI와 중계만 한다.
# BRAIN_URL 미설정이면 메뉴·화면이 아예 안 나온다(사이트 다른 기능에 영향 0 — HANDOFF 계약).
BRAIN_URL = os.environ.get("BRAIN_URL", "").rstrip("/")     # Cloudflare Tunnel 주소 (노트북 세션이 발급)
BRAIN_TOKEN = os.environ.get("BRAIN_TOKEN", "")             # 노트북 API 인증 토큰
# 사진 첨부(이미지 1단계, HANDOFF-ask-chat-r8) — 노트북 수신부가 아직 없어(r9 대기),
# 이 환경변수가 참일 때만 ①화면에 사진 버튼을 보이고 ②payload에 image 필드를 싣는다.
# 미설정(기본값)이면 사진 관련 코드는 전혀 타지 않아 기존 텍스트 전용 동작과 완전히 같다.
ASK_IMG_ON = bool(os.environ.get("BRAIN_IMG", ""))

# 사이드바가 어느 화면에서든 부르는 전역 함수들
templates.env.globals["recent_tickets"] = recent_tickets
templates.env.globals["get_user"] = require_user
templates.env.globals["ASK_ON"] = bool(BRAIN_URL)           # 상담 챗 메뉴 노출 여부
templates.env.globals["ASK_IMG_ON"] = ASK_IMG_ON            # 사진 첨부 UI 노출 여부


@app.get("/login")
def login_form(request: Request):
    """간이 로그인 — 목록에서 자기 이름을 고른다 (비밀번호 없음, 역할 시연용)."""
    con = db()
    staff = con.execute("""SELECT s.*, COALESCE(f.name, '본사') AS factory_name FROM staff s
        LEFT JOIN factories f ON f.id = s.factory_id
        ORDER BY CASE s.role WHEN 'owner' THEN 0 WHEN 'manager' THEN 1 WHEN 'driver' THEN 2 ELSE 3 END,
                 s.factory_id, s.name""").fetchall()
    con.close()
    return templates.TemplateResponse(request, "login.html",
                                      {"staff": staff, "ROLE_LABEL": ROLE_LABEL})


@app.post("/login")
def login_submit(staff_id: int = Form(...)):
    """선택한 담당자 번호를 쿠키에 담는다 — 이후 모든 화면이 이 쿠키로 범위를 정한다.
    도착지는 역할에 따라 다르다: 현장 인력(공장·기사)의 목적지는 관리 지표가 아니라
    「내 클레임」이므로 그리로 바로 보낸다 (2026-08-10 UI 점검 지적: 역할별 홈 위계)."""
    con = db()
    row = con.execute("SELECT role FROM staff WHERE id=?", (staff_id,)).fetchone()
    con.close()
    home = f"/me/{staff_id}" if row and row["role"] in ("factory", "driver") else "/"
    resp = RedirectResponse(home, status_code=303)
    resp.set_cookie("uid", str(staff_id))
    return resp


@app.get("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("uid")
    return resp


@app.get("/search")
def search(request: Request, q: str = ""):
    """상단 바 검색 — 티켓 내용·거래처 이름에서 글자 검색. 자기 공장 범위 안에서만."""
    user = require_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    fid = scope_of(user)
    con = db()
    rows = []
    if q.strip():
        like = f"%{q.strip()}%"
        rows = con.execute(TICKET_SELECT + " WHERE (c.content LIKE ? OR cl.name LIKE ?)"
                           + (" AND cl.factory_id=?" if fid else "")
                           + " ORDER BY c.created_at DESC",
                           ((like, like, fid) if fid else (like, like))).fetchall()
    con.close()
    return templates.TemplateResponse(request, "search.html", {
        "q": q, "rows": rows,
        "TYPE_LABEL": TYPE_LABEL, "STATUS_LABEL": STATUS_LABEL,
    })


@app.get("/")
def dashboard(request: Request, factory: int | None = None):
    """공장 대시보드 — 모니터에 띄워두는 화면. 미처리(urgent 먼저, 오래된 것 먼저)가 위.
    본사는 탭으로 공장을 고르고, 공장 사람은 자기 공장 것만 강제로 본다."""
    user = require_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    fid = scope_of(user)
    if fid:
        factory = fid          # 공장 사람은 탭 파라미터와 무관하게 자기 공장만
    con = db()
    fwhere = " AND cl.factory_id = ?" if factory else ""   # 공장 필터 조각
    fargs: tuple = (factory,) if factory else ()
    open_tickets = con.execute(TICKET_SELECT + " WHERE c.status != 'done'" + fwhere + """
        ORDER BY (c.severity = 'urgent') DESC, c.created_at ASC""", fargs).fetchall()
    # 최근 완료는 현황판의 완료 열과 같은 상한(5건) — 한 화면에서 완료 개수가
    # 7·8·5로 제각각 보이던 문제를 기준 통일로 없앤다 (2026-08-10 UI 점검 지적)
    # 완료 열이 길어 스크롤이 늘어지던 것을 8→5로 더 줄임(2026-08-11 UI 수정 21건 #4)
    done_recent = con.execute(TICKET_SELECT + " WHERE c.status = 'done'" + fwhere + """
        ORDER BY c.done_at DESC LIMIT 5""", fargs).fetchall()
    # 지표: 전화·문자가 절대 못 주는 숫자들 (공장 필터가 걸리면 그 공장 것만 센다)
    scoped = "FROM complaints c JOIN clients cl ON cl.id = c.client_id WHERE 1=1" + fwhere
    stats = {
        "open": len(open_tickets),
        "unacked": sum(1 for t in open_tickets if t["status"] == "new"),
        "today": con.execute(f"SELECT COUNT(*) {scoped} AND c.created_at LIKE ?",
                             fargs + (now()[:10] + "%",)).fetchone()[0],
        # 평균 처리 시간(시간 단위): 완료 티켓의 (완료시각-접수시각) 평균
        "avg_hours": con.execute(f"""SELECT {sql_avg_hours('c.done_at', 'c.created_at')}
                                     {scoped} AND c.status='done'""", fargs).fetchone()[0],
    }
    by_type = con.execute(f"SELECT c.type, COUNT(*) n {scoped} GROUP BY c.type ORDER BY n DESC",
                          fargs).fetchall()
    factories = con.execute("SELECT * FROM factories ORDER BY id").fetchall()
    # 카톡 알림 연동 여부 + 연동된 계정 표시 — 사장 화면에 상태·입구를 보여주기 위해
    kakao_on = bool(KAKAO_REST_KEY) and setting_get(con, "kakao_refresh_token") is not None
    kakao_who = setting_get(con, "kakao_account") or "연동됨"
    con.close()
    cols, days_old, photo_n = board_data(factory)   # 대시보드 상단의 칸반 (같은 공장 필터로)
    return templates.TemplateResponse(request, "dashboard.html", {
        "open_tickets": open_tickets, "done_recent": done_recent,
        "cols": cols, "days_old": days_old, "photo_n": photo_n,
        "stats": stats, "by_type": by_type,
        "factories": factories, "cur_factory": factory,
        "allow_all": scope_of(user) is None,   # 본사만 공장 탭을 본다
        "kakao_on": kakao_on, "kakao_who": kakao_who,
        "kakao_ready": bool(KAKAO_REST_KEY),   # 키 등록 전엔 연동 입구를 아예 숨긴다 (데모 방문자에게 설정 안내가 새지 않게)
        "TYPE_LABEL": TYPE_LABEL, "STATUS_LABEL": STATUS_LABEL,
    })


def board_data(fid: int | None, client_id: int | None = None):
    """칸반 보드의 재료 — 상태별 묶음·경과일·사진 개수.
    대시보드(공장 범위)·보드 화면·거래처 대시보드(업체 범위)가 같은 재료를 쓴다."""
    con = db()
    where, args = " WHERE 1=1", []
    if fid:
        where += " AND cl.factory_id=?"; args.append(fid)
    if client_id:
        where += " AND c.client_id=?"; args.append(client_id)
    rows = con.execute(TICKET_SELECT + where
                       + " ORDER BY (c.severity='urgent') DESC, c.created_at ASC",
                       tuple(args)).fetchall()
    # 티켓별 사진 개수 (카드에 📷 표시용)
    photo_n = dict(con.execute("""SELECT a.complaint_id, COUNT(*) FROM photos p
        JOIN actions a ON a.id = p.action_id GROUP BY a.complaint_id""").fetchall())
    con.close()
    # 상태별로 묶고, 완료 열은 최근 5건만 (완료가 쌓이면 열이 무한히 길어지므로 — 8건이던 것을
    # 2026-08-11 UI 수정 21건 #4에서 5건으로 더 줄였다. done_recent와 상한을 맞춰야
    # 한 화면(대시보드)에서 완료 개수가 다르게 보이는 일이 다시 생기지 않는다)
    cols = {s: [] for s in ("new", "acked", "working", "done")}
    today = datetime.now(KST).replace(tzinfo=None)   # created_at(한국 시간 문자열)과 같은 기준으로 경과일 계산
    days_old = {}
    for t in rows:
        cols[t["status"]].append(t)
        created = datetime.strptime(t["created_at"], "%Y-%m-%d %H:%M")
        days_old[t["id"]] = (today - created).days
    cols["done"] = sorted(cols["done"], key=lambda t: t["done_at"] or "", reverse=True)[:5]
    return cols, days_old, photo_n


@app.get("/board")
def board():
    """예전엔 칸반 보드 전용 화면이었다 — 지금은 대시보드(/) 상단 칸반과 내용이 완전히 겹쳐서
    어디서도 링크되지 않는 고아 페이지가 됐다(2026-08-11 점검). 화면을 지우는 대신 옛 주소가
    죽지 않게 대시보드로 돌려보내기만 한다(북마크·외부 링크가 있었을 가능성 대비)."""
    return RedirectResponse("/", status_code=303)


@app.post("/c/{ticket_id}/status")
def set_status(request: Request, ticket_id: int, status: str = Form(...)):
    """보드에서 카드를 끌어다 놓으면 상태를 바꾼다 — 조치 일지에도 한 줄 남긴다(추적 원칙)."""
    user = require_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if status not in STATUS_LABEL:
        return RedirectResponse("/board", status_code=303)
    # 이동은 'move' 한 종류로만 기록 — 예전엔 ack/work로 적어서 드래그가 「처리 보고」처럼 보였다(재검증 지적)
    move_txt = {"new": "확인 대기로", "acked": "확인됨으로", "working": "처리중으로", "done": "완료로"}[status]
    con = db()
    con.execute("INSERT INTO actions (complaint_id, actor, kind, content, created_at) VALUES (?,?,?,?,?)",
                (ticket_id, user["name"], "move", f"현황판에서 {move_txt} 이동", now()))
    con.execute("UPDATE complaints SET status=?, done_at=? WHERE id=?",
                (status, now() if status == "done" else None, ticket_id))
    con.commit(); con.close()
    return RedirectResponse("/board", status_code=303)


@app.get("/client/{client_id}")
def client_detail(request: Request, client_id: int):
    """거래처 상세 — 이 업체의 이슈가 무엇이 있고 어떻게 처리되고 있는지 한 화면에 추적한다."""
    user = require_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    con = db()
    c = con.execute("""SELECT c.*, f.name AS factory_name FROM clients c
        LEFT JOIN factories f ON f.id = c.factory_id WHERE c.id=?""", (client_id,)).fetchone()
    fid = scope_of(user)
    if c is None or (fid and c["factory_id"] != fid):   # 남의 공장 거래처는 못 본다
        con.close()
        return RedirectResponse("/clients", status_code=303)
    goods = con.execute("""SELECT i.name, ci.daily_qty FROM client_items ci
        JOIN items i ON i.id = ci.item_id WHERE ci.client_id=? ORDER BY ci.daily_qty DESC""",
        (client_id,)).fetchall()
    open_tickets = con.execute(TICKET_SELECT + """
        WHERE c.client_id=? AND c.status != 'done'
        ORDER BY (c.severity='urgent') DESC, c.created_at ASC""", (client_id,)).fetchall()
    done_tickets = con.execute(TICKET_SELECT + """
        WHERE c.client_id=? AND c.status = 'done' ORDER BY c.done_at DESC""", (client_id,)).fetchall()
    # 업체별 지표: 누적 건수 · 유형별 · 평균 처리 시간 (반복되는 문제가 있는 거래처를 숫자로 보려고)
    stats = {
        "total": len(open_tickets) + len(done_tickets),
        "avg_hours": con.execute(f"""SELECT {sql_avg_hours('done_at', 'created_at')}
                                    FROM complaints WHERE client_id=? AND status='done'""",
                                 (client_id,)).fetchone()[0],
    }
    by_type = con.execute("""SELECT type, COUNT(*) n FROM complaints
                             WHERE client_id=? GROUP BY type ORDER BY n DESC""", (client_id,)).fetchall()
    con.close()
    cols, days_old, photo_n = board_data(None, client_id)   # 이 업체만의 현황판
    return templates.TemplateResponse(request, "client.html", {
        "c": c, "goods": goods, "open_tickets": open_tickets, "done_tickets": done_tickets,
        "stats": stats, "by_type": by_type,
        "cols": cols, "days_old": days_old, "photo_n": photo_n,
        "TYPE_LABEL": TYPE_LABEL, "STATUS_LABEL": STATUS_LABEL,
    })


@app.get("/f/{factory_id}")
def factory_detail(request: Request, factory_id: int):
    """공장 상세 — 이 공장의 거래처(취급 품목·특이사항), 현재 이슈, 인력 명단을 한 화면에."""
    user = require_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    fid = scope_of(user)
    if fid and factory_id != fid:                        # 남의 공장 페이지는 자기 공장으로 돌려보낸다
        return RedirectResponse(f"/f/{fid}", status_code=303)
    con = db()
    factory = con.execute("SELECT * FROM factories WHERE id=?", (factory_id,)).fetchone()
    # 거래처 + 각자의 취급 품목 (많은 순으로)
    client_rows = []
    for c in con.execute("SELECT * FROM clients WHERE factory_id=? ORDER BY daily_kg DESC",
                         (factory_id,)).fetchall():
        its = con.execute("""SELECT i.name, ci.daily_qty FROM client_items ci
            JOIN items i ON i.id = ci.item_id
            WHERE ci.client_id=? ORDER BY ci.daily_qty DESC""", (c["id"],)).fetchall()
        client_rows.append({"c": c, "goods": its})   # 이름이 items면 딕셔너리 내장 기능과 겹쳐 사고남
    total_kg = sum(r["c"]["daily_kg"] for r in client_rows)
    # 현재 이슈 = 이 공장 거래처의 미완료 티켓
    open_tickets = con.execute(TICKET_SELECT + """
        WHERE c.status != 'done' AND cl.factory_id = ?
        ORDER BY (c.severity='urgent') DESC, c.created_at ASC""", (factory_id,)).fetchall()
    # 인력 명단 + 각자의 미완료·긴급 배정 건수
    staff = con.execute("SELECT * FROM staff WHERE factory_id=?", (factory_id,)).fetchall()
    open_by_staff = dict(con.execute("""SELECT assignee_id, COUNT(*) FROM complaints
        WHERE status != 'done' AND assignee_id IS NOT NULL GROUP BY assignee_id""").fetchall())
    urgent_by_staff = dict(con.execute("""SELECT assignee_id, COUNT(*) FROM complaints
        WHERE status != 'done' AND severity = 'urgent' AND assignee_id IS NOT NULL
        GROUP BY assignee_id""").fetchall())
    con.close()
    # 정렬: 「긴급 보유 우선 → 미완료 많은 순 → 이름」 — me_home과 같은 기준(2026-08-11 UI 수정 #20).
    # (예전엔 공장장→기사→인력 순 고정이었는데, 급한 건을 든 사람이 직급과 무관하게 먼저 보여야
    #  이 화면만 보고도 누구부터 챙길지 판단할 수 있다)
    staff = sorted(staff, key=lambda s: (
        0 if urgent_by_staff.get(s["id"]) else 1,
        -open_by_staff.get(s["id"], 0),
        s["name"],
    ))
    return templates.TemplateResponse(request, "factory.html", {
        "f": factory, "client_rows": client_rows, "total_kg": total_kg,
        "open_tickets": open_tickets, "staff": staff, "open_by_staff": open_by_staff,
        "TYPE_LABEL": TYPE_LABEL, "STATUS_LABEL": STATUS_LABEL, "ROLE_LABEL": ROLE_LABEL,
    })


@app.get("/new")
def new_form(request: Request, client_id: int | None = None):
    """컴플레인 접수 폼 — 거래처·담당자 목록을 공장별로 묶어, 자기 공장 범위 안에서만.
    client_id가 쿼리로 오면(거래처 상세 화면의 「이 거래처로 접수」 딥링크) 그 거래처를
    select에서 미리 골라 둔다 — 매번 목록에서 다시 찾는 수고를 던다(2026-08-11 UI 수정 #5)."""
    user = require_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    fid = scope_of(user)
    fw = " WHERE c.factory_id=?" if fid else ""
    sw = " WHERE s.factory_id=?" if fid else " WHERE s.role != 'owner'"   # 본사는 배정 대상이 아니다
    args = (fid,) if fid else ()
    con = db()
    clients = con.execute("""SELECT c.*, f.name AS factory_name FROM clients c
        LEFT JOIN factories f ON f.id = c.factory_id""" + fw + """
        ORDER BY c.factory_id, c.daily_kg DESC""", args).fetchall()
    staff = con.execute("""SELECT s.*, f.name AS factory_name FROM staff s
        LEFT JOIN factories f ON f.id = s.factory_id""" + sw + """
        ORDER BY s.factory_id, CASE s.role WHEN 'manager' THEN 0 WHEN 'driver' THEN 1 ELSE 2 END, s.name""",
        args).fetchall()
    con.close()
    return templates.TemplateResponse(request, "new.html", {
        "clients": clients, "staff": staff, "sel_client_id": client_id,
        "TYPE_LABEL": TYPE_LABEL, "ROLE_LABEL": ROLE_LABEL,
    })


@app.get("/clients")
def client_list(request: Request):
    """거래처 현황 — 공장별로 묶어 업태·객실수·하루 물량 추정을. 자기 공장 범위 안에서만."""
    user = require_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    fid = scope_of(user)
    con = db()
    factories = con.execute("SELECT * FROM factories" + (" WHERE id=?" if fid else "") + " ORDER BY id",
                            (fid,) if fid else ()).fetchall()
    clients = con.execute("""SELECT c.*, f.name AS factory_name FROM clients c
        LEFT JOIN factories f ON f.id = c.factory_id"""
        + (" WHERE c.factory_id=?" if fid else "") + """
        ORDER BY c.factory_id, c.daily_kg DESC""", (fid,) if fid else ()).fetchall()
    # 공장별 하루 물량 합계 (인력 대비 처리량을 한눈에 보려고)
    totals = {f["id"]: sum(c["daily_kg"] for c in clients if c["factory_id"] == f["id"])
              for f in factories}
    con.close()
    return templates.TemplateResponse(request, "clients.html", {
        "factories": factories, "clients": clients, "totals": totals,
    })


@app.post("/new")
def new_submit(request: Request, client_id: int = Form(...), type: str = Form(...),
               severity: str = Form("normal"), content: str = Form(...),
               channel: str = Form("전화"), assignee_id: int = Form(...),
               instruction: str = Form(""), photos: list[UploadFile] = File([])):
    """접수 처리: 티켓 생성 + 접수 기록(사진 여러 장 가능) + (지시를 적었으면) 지시 기록 + 담당자 알림(자리).
    접수자·지시자 이름은 로그인한 사람 것을 쓴다 — 폼에서 고르는 게 아니라."""
    user = require_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    con = db()
    # 거래처와 담당자의 소속 공장이 다르면 접수를 막는다 — 화면(JS)에서 이미 다른 공장 담당자를
    # 골라진 못하게 숨기지만, 폼을 조작하거나 JS가 꺼진 요청도 있을 수 있어 서버가 최종 확인한다.
    # 담당자 factory_id가 NULL(본사)이면 어느 거래처든 배정 가능 — 예외.
    client_row = con.execute("SELECT factory_id FROM clients WHERE id=?", (client_id,)).fetchone()
    assignee_row = con.execute("SELECT factory_id FROM staff WHERE id=?", (assignee_id,)).fetchone()
    if (client_row is None or assignee_row is None
            or (assignee_row["factory_id"] is not None
                and assignee_row["factory_id"] != client_row["factory_id"])):
        con.close()
        return HTMLResponse("거래처와 담당자의 소속 공장이 다릅니다. 뒤로 가서 다시 선택해 주세요.",
                            status_code=400)
    ticket_id = insert_id(con, """INSERT INTO complaints
        (client_id, type, severity, content, channel, assignee_id, status, created_at)
        VALUES (?,?,?,?,?,?, 'new', ?)""",
        (client_id, type, severity, content, channel, assignee_id, now()))
    reg_id = insert_id(con, "INSERT INTO actions (complaint_id, actor, kind, content, created_at) VALUES (?,?,?,?,?)",
                       (ticket_id, user["name"], "register", f"{channel} 접수", now()))
    attach_photos(con, reg_id, photos)
    if instruction.strip():
        con.execute("INSERT INTO actions (complaint_id, actor, kind, content, created_at) VALUES (?,?,?,?,?)",
                    (ticket_id, user["name"], "instruct", instruction.strip(), now()))
    assignee = con.execute("SELECT name FROM staff WHERE id=?", (assignee_id,)).fetchone()
    con.commit(); con.close()
    notify(assignee["name"], f"새 클레임 #{ticket_id} — {content.strip()[:60]}", ticket_id)
    return RedirectResponse(f"/c/{ticket_id}", status_code=303)


@app.get("/c/{ticket_id}")
def ticket_detail(request: Request, ticket_id: int):
    """티켓 상세 — 정보 + 조치 일지(시간순) + 조치 입력 폼."""
    user = require_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    con = db()
    ticket = con.execute(TICKET_SELECT + " WHERE c.id=?", (ticket_id,)).fetchone()
    fid = scope_of(user)
    if ticket is None or (fid and con.execute(
            "SELECT factory_id FROM clients WHERE id=?", (ticket["client_id"],)
            ).fetchone()[0] != fid):                     # 남의 공장 티켓은 못 본다
        con.close()
        return RedirectResponse("/", status_code=303)
    acts = con.execute("SELECT * FROM actions WHERE complaint_id=? ORDER BY created_at, id",
                       (ticket_id,)).fetchall()
    # 조치별 사진 목록: {조치 id: [파일명, ...]} — 화면에서 각 조치 밑에 사진들을 붙이려고
    photo_map = {}
    for row in con.execute("""SELECT action_id, filename FROM photos
        WHERE action_id IN (SELECT id FROM actions WHERE complaint_id=?)""", (ticket_id,)):
        photo_map.setdefault(row["action_id"], []).append(row["filename"])
    # 조치별 수정 이력: {조치 id: [이전 내용 줄, ...]} — 수정된 조치 밑에 원문을 접어 보여준다
    edits_map = {}
    for row in con.execute("""SELECT action_id, old_content, edited_at FROM action_edits
        WHERE action_id IN (SELECT id FROM actions WHERE complaint_id=?)
        ORDER BY edited_at, id""", (ticket_id,)):
        edits_map.setdefault(row["action_id"], []).append(row)
    staff = con.execute("SELECT * FROM staff ORDER BY role, name").fetchall()
    can_delete = deletable_ticket(con, ticket_id, user)   # 오접수 삭제 버튼 노출 여부
    con.close()
    return templates.TemplateResponse(request, "ticket.html", {
        "t": ticket, "acts": acts, "staff": staff, "photo_map": photo_map,
        "edits_map": edits_map, "EDITABLE_KINDS": EDITABLE_KINDS, "can_delete": can_delete,
        "TYPE_LABEL": TYPE_LABEL, "STATUS_LABEL": STATUS_LABEL, "KIND_LABEL": KIND_LABEL,
    })


# 조치 종류 → 티켓 상태 자동 변경 규칙 (확인하면 '확인됨', 처리 보고하면 '처리중'…)
KIND_TO_STATUS = {"ack": "acked", "work": "working", "done": "done"}

# 수정·삭제를 허용하는 조치 종류 — 사람이 손으로 쓴 것만. register(접수)·ack(지시 확인)·
# move(상태 이동)는 버튼이 만든 기계 기록이라 고칠 이유도, 고치게 둘 이유도 없다.
EDITABLE_KINDS = ("instruct", "work", "done", "note")


def deletable_ticket(con, ticket_id: int, user) -> bool:
    """오접수 삭제 자격 — ①접수 기록을 쓴 본인이면서 ②티켓의 모든 기록이 본인 것일 때만.
    남의 기록(지시 확인·처리 보고 등)이 하나라도 붙었으면 불가 — 그때부터는 여럿의 공유
    이력이라 삭제가 아니라 완료 처리+메모로 정리한다 (2026-08-12 인각님 결정)."""
    reg = con.execute("""SELECT actor FROM actions WHERE complaint_id=? AND kind='register'
                         ORDER BY id LIMIT 1""", (ticket_id,)).fetchone()
    if reg is None or reg["actor"] != user["name"]:
        return False
    others = con.execute("SELECT COUNT(*) FROM actions WHERE complaint_id=? AND actor != ?",
                         (ticket_id, user["name"])).fetchone()[0]
    return others == 0


def own_editable_action(con, action_id: int, user):
    """수정·삭제 자격 검사 — ①존재하고 ②아직 안 지웠고 ③손으로 쓴 종류이고 ④본인 글일 때만
    그 조치 줄을 돌려준다. 하나라도 어긋나면 None. 남의 글은 role 불문 못 건드린다
    (2026-08-12 인각님 결정: 타인 수정은 불가, 본인 것만 — 대신 원문 이력이 남는다)."""
    a = con.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
    if (a is None or a["deleted_at"] or a["kind"] not in EDITABLE_KINDS
            or a["actor"] != user["name"]):
        return None
    return a


@app.post("/c/{ticket_id}/action")
def add_action(request: Request, ticket_id: int, kind: str = Form(...),
               content: str = Form(""), photos: list[UploadFile] = File([])):
    """조치 기록 추가 — 일지에 한 줄 쌓고(사진 여러 장 가능), 종류에 따라 티켓 상태를 옮긴다.
    「누가」는 폼에서 받지 않고 로그인한 사람으로 박는다 — 남 이름으로 기록 못 하게."""
    user = require_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    con = db()
    aid = insert_id(con, "INSERT INTO actions (complaint_id, actor, kind, content, created_at) VALUES (?,?,?,?,?)",
                    (ticket_id, user["name"], kind, content.strip(), now()))
    attach_photos(con, aid, photos)
    if kind in KIND_TO_STATUS:
        new_status = KIND_TO_STATUS[kind]
        con.execute("UPDATE complaints SET status=? WHERE id=?", (new_status, ticket_id))
        if new_status == "done":
            con.execute("UPDATE complaints SET done_at=? WHERE id=?", (now(), ticket_id))
    con.commit(); con.close()
    return RedirectResponse(f"/c/{ticket_id}", status_code=303)


@app.get("/a/{action_id}/edit")
def edit_action_form(request: Request, action_id: int):
    """조치 수정 화면 — 본인 글만 들어올 수 있다. 삭제 버튼도 이 화면에 함께 둔다
    (타임라인에 삭제 버튼을 바로 두면 오탭 위험 — 화면 하나를 거치는 것이 마찰 겸 확인)."""
    user = require_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    con = db()
    a = own_editable_action(con, action_id, user)
    if a is None:
        con.close()
        return RedirectResponse("/", status_code=303)
    edits = con.execute("SELECT * FROM action_edits WHERE action_id=? ORDER BY edited_at, id",
                        (action_id,)).fetchall()
    con.close()
    return templates.TemplateResponse(request, "edit_action.html",
                                      {"a": a, "edits": edits, "KIND_LABEL": KIND_LABEL})


@app.post("/a/{action_id}/edit")
def edit_action_submit(request: Request, action_id: int, content: str = Form(...)):
    """조치 수정 저장 — 원문을 action_edits에 먼저 남기고 나서 바꾼다 (증거 보존이 먼저).
    빈 내용이나 그대로면 아무것도 안 바꾼다 — 지우고 싶으면 삭제 버튼을 쓰는 것."""
    user = require_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    con = db()
    a = own_editable_action(con, action_id, user)
    if a is None:
        con.close()
        return RedirectResponse("/", status_code=303)
    new = content.strip()
    if new and new != a["content"]:
        con.execute("INSERT INTO action_edits (action_id, old_content, edited_at) VALUES (?,?,?)",
                    (action_id, a["content"], now()))
        con.execute("UPDATE actions SET content=?, edited_at=? WHERE id=?",
                    (new, now(), action_id))
        con.commit()
    con.close()
    return RedirectResponse(f"/c/{a['complaint_id']}", status_code=303)


@app.post("/a/{action_id}/delete")
def delete_action(request: Request, action_id: int):
    """조치 삭제 — 줄을 지우지 않고 deleted_at만 찍는다. 화면에는 취소선으로 남는다.
    티켓 상태는 안 되돌린다 — 상태가 틀어졌으면 현황판에서 옮기면 된다(자동 되돌리기는
    다른 사람의 후속 기록과 얽혀 더 큰 사고를 만든다)."""
    user = require_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    con = db()
    a = own_editable_action(con, action_id, user)
    if a is None:
        con.close()
        return RedirectResponse("/", status_code=303)
    con.execute("UPDATE actions SET deleted_at=? WHERE id=?", (now(), action_id))
    con.commit(); con.close()
    return RedirectResponse(f"/c/{a['complaint_id']}", status_code=303)


@app.post("/c/{ticket_id}/delete")
def delete_ticket(request: Request, ticket_id: int):
    """오접수 티켓 삭제 — 조치 삭제(취소선)와 달리 여기는 완전 삭제다.
    자격(deletable_ticket)이 「본인 기록밖에 없는 티켓」만 통과시키므로 남의 흔적을 지우는
    일이 없고, 오접수가 취소선으로 목록·통계에 남으면 건수·처리 시간 지표를 오염시킨다."""
    user = require_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    con = db()
    if not deletable_ticket(con, ticket_id, user):
        con.close()
        return RedirectResponse(f"/c/{ticket_id}", status_code=303)
    # 지우기 전에 첨부 파일명을 걷어 둔다 — 줄을 지우면 못 찾는다
    files = [r["filename"] for r in con.execute(
        """SELECT filename FROM photos WHERE action_id IN
           (SELECT id FROM actions WHERE complaint_id=?)""", (ticket_id,))]
    # 자식 → 부모 순서로 지운다 (참조 제약)
    con.execute("""DELETE FROM photos WHERE action_id IN
                   (SELECT id FROM actions WHERE complaint_id=?)""", (ticket_id,))
    con.execute("""DELETE FROM action_edits WHERE action_id IN
                   (SELECT id FROM actions WHERE complaint_id=?)""", (ticket_id,))
    con.execute("DELETE FROM actions WHERE complaint_id=?", (ticket_id,))
    con.execute("DELETE FROM complaints WHERE id=?", (ticket_id,))
    con.commit(); con.close()
    for f in files:                       # 파일은 DB 정리가 끝난 뒤에 — 실패해도 고아 파일일 뿐
        if not f.startswith("demo_"):     # 데모 사진은 여러 예시 티켓이 같이 쓰는 공유 자원
            (UPLOAD_DIR / f).unlink(missing_ok=True)
    return RedirectResponse("/", status_code=303)


@app.get("/me")
def me_home(request: Request):
    """내 티켓 입구 — 나는 내 것으로 바로, 본사만 담당자를 골라 볼 수 있다."""
    user = require_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if user["role"] != "owner":
        return RedirectResponse(f"/me/{user['id']}", status_code=303)
    con = db()
    # 공장 이름을 붙여 공장별로 묶어 보여준다 (로그인 화면과 같은 배열 — 찾기 쉽게)
    staff = con.execute("""SELECT s.*, COALESCE(f.name, '본사') AS factory_name FROM staff s
        LEFT JOIN factories f ON f.id = s.factory_id
        WHERE s.role != 'owner'
        ORDER BY s.factory_id, CASE s.role WHEN 'manager' THEN 0 WHEN 'driver' THEN 1 ELSE 2 END, s.name""").fetchall()
    # 담당자별 현재 부담 — "누가 몇 건을 들고 있나"를 목록에서 바로 보이게 (2026-08-10 인각님 요청)
    open_n = dict(con.execute("""SELECT assignee_id, COUNT(*) FROM complaints
        WHERE status != 'done' AND assignee_id IS NOT NULL GROUP BY assignee_id""").fetchall())
    urgent_n = dict(con.execute("""SELECT assignee_id, COUNT(*) FROM complaints
        WHERE status != 'done' AND severity = 'urgent' AND assignee_id IS NOT NULL
        GROUP BY assignee_id""").fetchall())
    con.close()
    # 목록 정렬: 「긴급 보유 우선 → 미완료 많은 순 → 이름」 — 급한 사람이 눈에 먼저 띄어야
    # 사장이 누구부터 챙길지 목록만 보고 판단할 수 있다(2026-08-11 UI 수정 #20).
    # 공장별 묶음(groupby)은 그대로 유지해야 하므로 factory_id를 정렬 키 맨 앞에 둬
    # 공장 경계를 넘어 뒤섞이지 않게 한다(Jinja groupby는 같은 키가 붙어 있어야 한 묶음으로 본다).
    staff = sorted(staff, key=lambda s: (
        s["factory_id"] if s["factory_id"] is not None else -1,
        0 if urgent_n.get(s["id"]) else 1,          # 긴급 보유가 먼저(0 < 1)
        -open_n.get(s["id"], 0),                    # 미완료 많은 순
        s["name"],
    ))
    return templates.TemplateResponse(request, "me_select.html",
                                      {"staff": staff, "ROLE_LABEL": ROLE_LABEL,
                                       "open_n": open_n, "urgent_n": urgent_n})


@app.get("/me/{staff_id}")
def my_tickets(request: Request, staff_id: int):
    """내 티켓 — 배정된 미완료 티켓. 기사가 폰으로 여는 화면.
    볼 수 있는 사람: 본인 · 본사(전부) · 공장장(자기 공장 인력만)."""
    user = require_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    con = db()
    me = con.execute("SELECT * FROM staff WHERE id=?", (staff_id,)).fetchone()
    allowed = (user["role"] == "owner" or user["id"] == staff_id
               or (user["role"] == "manager" and me and me["factory_id"] == user["factory_id"]))
    if me is None or not allowed:
        con.close()
        return RedirectResponse(f"/me/{user['id']}", status_code=303)
    tickets = con.execute(TICKET_SELECT + """
        WHERE c.assignee_id=? AND c.status != 'done'
        ORDER BY (c.severity='urgent') DESC, c.created_at ASC""", (staff_id,)).fetchall()
    done_tickets = con.execute(TICKET_SELECT + """
        WHERE c.assignee_id=? AND c.status = 'done'
        ORDER BY c.done_at DESC LIMIT 10""", (staff_id,)).fetchall()
    # 티켓마다 지시 내용과 접수 사진을 같이 보여준다 (기사가 봐야 할 핵심)
    instructions = {}
    for t in tickets:
        rows = con.execute("""SELECT id, kind, content, created_at FROM actions
            WHERE complaint_id=? AND kind IN ('register','instruct')
              AND deleted_at IS NULL
            ORDER BY created_at""", (t["id"],)).fetchall()   # 삭제된 지시는 기사에게 안 보낸다
        items = []
        for r in rows:
            photos = [p["filename"] for p in
                      con.execute("SELECT filename FROM photos WHERE action_id=?", (r["id"],))]
            if r["kind"] == "instruct" or photos:   # 접수 줄은 사진이 있을 때만 보여준다
                items.append({"kind": r["kind"], "content": r["content"],
                              "photos": photos, "created_at": r["created_at"]})
        instructions[t["id"]] = items
    con.close()
    return templates.TemplateResponse(request, "me.html", {
        "me": me, "tickets": tickets, "done_tickets": done_tickets,
        "instructions": instructions, "is_self": user["id"] == staff_id,
        "TYPE_LABEL": TYPE_LABEL, "STATUS_LABEL": STATUS_LABEL, "ROLE_LABEL": ROLE_LABEL,
    })


@app.get("/ask")
def ask_page(request: Request):
    """세탁 상담 챗 — 상담 목록(방 여러 개, 카톡 채팅 목록 느낌) 화면.
    한 사람이 주제별로 방을 나눠 물어볼 수 있게 되면서(HANDOFF-ask-chat.md 확장),
    /ask는 방을 고르는 입구가 되고 실제 대화는 /ask/chat이 맡는다."""
    user = require_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if not BRAIN_URL:                        # 메뉴가 숨겨져 있어도 주소로 직접 오면 안내
        return HTMLResponse("AI 채팅은 준비 중입니다. <p><a href='/'>← 대시보드로</a></p>")
    con = db()
    # 내 방 목록 — 방마다 쌓인 메시지 개수(n)를 같이 세어 카드에 보여준다.
    # 최신 방이 위로 오도록 id 역순(가장 최근에 만든 방 = 가장 큰 id).
    chats = con.execute("""SELECT c.id, c.title, c.created_at, COUNT(m.id) AS n
        FROM chats c LEFT JOIN chat_msgs m ON m.chat_id=c.id
        WHERE c.staff_id=? GROUP BY c.id, c.title, c.created_at
        ORDER BY c.id DESC""", (user["id"],)).fetchall()
    con.close()
    return templates.TemplateResponse(request, "ask_list.html", {"chats": chats})


@app.get("/ask/chat")
def ask_chat(request: Request, id: int = 0):
    """세탁 상담 챗 — 실제 대화 화면. id=0이면 아직 DB에 없는 새 상담(빈 방 —
    첫 질문을 보내는 순간에야 /api/ask가 방을 만든다). id가 있으면 그 방의 저장된
    대화를 시간순으로 불러와 서버에서 그대로 그린다(새로고침해도 대화가 안 사라진다)."""
    user = require_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if not BRAIN_URL:                        # 메뉴가 숨겨져 있어도 주소로 직접 오면 안내
        return HTMLResponse("AI 채팅은 준비 중입니다. <p><a href='/'>← 대시보드로</a></p>")
    msgs: list[dict] = []
    con = db()
    if id:
        chat = con.execute("SELECT * FROM chats WHERE id=?", (id,)).fetchone()
        if chat is None or chat["staff_id"] != user["id"]:   # 없는 방이거나 남의 방 — 목록으로 돌려보낸다
            con.close()
            return RedirectResponse("/ask", status_code=303)
        rows = con.execute("SELECT * FROM chat_msgs WHERE chat_id=? ORDER BY id", (id,)).fetchall()
        # sources 칸은 DB에 JSON 문자열로 눌러 담겨 있으므로 화면에 넘기기 전에 풀어준다.
        # (빈 문자열 = 근거 없음 → 빈 목록. bot 메시지가 아니면 애초에 이 칸이 비어 있다)
        for r in rows:
            msgs.append({"role": r["role"], "content": r["content"], "ok": r["ok"],
                        "sources": json.loads(r["sources"]) if r["sources"] else []})
    # 데스크톱 화면에 붙는 대화 목록 사이드바용 — ask_page(/ask 목록 화면)가 쓰는 것과 같은 쿼리로
    # 내 방 전체(제목·메시지 개수)를 가져온다. id가 0(새 상담)이든 있든 항상 실행한다 — 새 상담
    # 화면에서도 사이드바에는 기존 대화들이 보여야 바로 옮겨갈 수 있다. 폰 화면은 좁아서 사이드바를
    # CSS로 숨기고(.asknav{display:none}) 그 대신 이미 있는 /ask 목록 화면이 방 고르는 역할을 맡는다.
    chats = con.execute("""SELECT c.id, c.title, c.created_at, COUNT(m.id) AS n
        FROM chats c LEFT JOIN chat_msgs m ON m.chat_id=c.id
        WHERE c.staff_id=? GROUP BY c.id, c.title, c.created_at
        ORDER BY c.id DESC""", (user["id"],)).fetchall()
    con.close()
    return templates.TemplateResponse(request, "ask.html", {"chat_id": id, "msgs": msgs, "chats": chats})


def shrink_photo(raw: bytes) -> bytes | None:
    """채팅에 첨부된 사진 원본 바이트를 축소·재인코딩한다 (계약 HANDOFF-ask-chat-r8 §2·§3 그대로).
    실패하거나(사진이 아님) 재인코딩 후에도 너무 크면 None을 돌려준다 — 호출부가 "사진을
    못 읽었다" 안내로 처리한다.
    전송 형식은 항상 JPEG로 고정한다(r8 §2-1) — 노트북 수신부가 PNG·WebP 분기를 만들 필요가
    없도록, 사용자가 무슨 형식으로 올리든 프록시가 여기서 한 가지로 통일해 내보낸다."""
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()                          # 실제로 픽셀을 읽어봐야 손상된 파일도 여기서 걸러진다
    except Exception:
        return None                         # 이미지가 아니거나 손상됨
    # ⚠️ 순서 중요 — exif_transpose를 축소보다 먼저 해야 한다. 아래서 저장할 때 exif 인자를
    # 안 줘서 회전 태그(Orientation)까지 통째로 지우는데, 그 전에 태그가 말하는 방향대로
    # 픽셀 자체를 미리 돌려놓지 않으면 세로로 찍은 폰 사진이 누워서 저장된다(r8 §2-2).
    img = ImageOps.exif_transpose(img)
    # PNG 투명 배경·팔레트 모드는 그대로 JPEG로 저장하면 색이 깨지므로 RGB로 통일한다
    img = img.convert("RGB")
    img.thumbnail((1600, 1600))             # 긴 변 1600px 이하로 축소 (비율 유지, 확대는 안 함)
    buf = io.BytesIO()
    # exif 인자를 안 주는 것만으로 메타데이터(제조사·GPS·회전 태그 등)가 전부 사라진다
    # — 실측 확인됨(r8 §2-2). 별도의 EXIF 삭제 코드가 필요 없다.
    img.save(buf, format="JPEG", quality=85)
    data = buf.getvalue()
    if len(data) > 4 * 1024 * 1024:         # 4MB 한도는 base64 문자열이 아니라 이 재인코딩된
        return None                          # JPEG 바이트 기준이다(r8 §2-3) — 1600px·q85면 보통 여유가 크다
    return data


@app.post("/api/ask")
def ask_proxy(request: Request, question: str = Form(""), chat_id: int = Form(0),
              photo: UploadFile | None = File(None)):
    # question의 기본값이 ""인 이유: FastAPI는 multipart의 "빈 필드"를 누락(missing)으로
    # 취급해서, 필수(Form(...))로 두면 사진만 보내는 전송(글 없음)이 422로 튕긴다 —
    # 검수 중 실측으로 발견. 빈 질문의 거절은 아래 길이 검증(2~500자)이 이미 맡고 있다.
    """상담 질문 중계 — 로그인 확인 → 길이 검증 → 방 확보(0이면 새로 만들고, 아니면 내 방인지
    확인) → 질문을 chat_msgs에 저장 → 노트북(BRAIN_URL)으로 전달 → 답을 그대로 반환하며
    chat_msgs에도 저장한다. 방 하나에 「질문 한 줄 + 답 한 줄」이 시간순으로 계속 쌓인다.
    답 속의 「※ 현장 검증 전」 딱지와 「⛔」 경고는 안전·정직성 장치라 절대 가공하지 않는다(HANDOFF 계약).
    photo: 사진 첨부(이미지 1단계) — ASK_IMG_ON이 꺼져 있으면(BRAIN_IMG 미설정) 화면에서
    애초에 이 필드를 보내지 않으므로, 아래 사진 처리 분기는 전혀 실행되지 않고 기존 텍스트
    전용 동작과 완전히 같다(회귀 0, HANDOFF 계약)."""
    user = require_user(request)
    if user is None:
        return JSONResponse({"ok": False, "error": "로그인이 필요합니다"}, status_code=401)
    q = question.strip()
    # 사진 읽기 + 축소 — 질문 길이 검증보다 먼저 한다: 사진만 보내고 글이 비어 있어도
    # 통과시켜야 하는데(아래 "(사진만 보냄)" 대체), 그러려면 사진이 유효한지부터 알아야 한다.
    img_b64 = None
    # 사진 처리에 실패했을 때 담을 안내 문구 — None이면 사진이 없었거나 정상 처리된 것.
    # 이 값이 있으면 아래에서 두뇌를 호출하지 않고, "두뇌 호출이 실패했을 때"와 완전히 같은
    # 자리(방 확보 → 질문 저장 → 이 문구를 봇 답으로 ok=0 저장 → 반환)로 흘려보낸다 —
    # 사진이 있었다는 사실 자체는 방에 남아야 하므로, 방을 만들기도 전에 끊어버리지 않는다.
    photo_error = None
    if ASK_IMG_ON and photo is not None and photo.filename:
        raw = photo.file.read()             # 동기 라우트라 await 없이 그냥 읽는다
        if raw:
            shrunk = shrink_photo(raw)
            if shrunk is None:
                photo_error = "사진을 읽지 못했습니다. JPG·PNG 사진인지, 너무 크지 않은지 확인해 주세요."
            else:
                img_b64 = base64.b64encode(shrunk).decode("ascii")
    # 질문 대체 문구(계약 r7 §2) — 사진이 유효하게 첨부됐고 글이 비어 있으면 이 문구로 통일한다.
    # 저장(chat_msgs)·history·두뇌 전송(payload["question"]) 모두 이 문구 하나를 기준으로 삼아야
    # 새로고침 전후, 다음 턴의 이력에서 보이는 모양이 전부 같아진다.
    if img_b64 and not q:
        q = "(사진만 보냄)"
    elif photo_error and not q:
        # 사진 처리 자체가 실패했을 때 — 사진만 보냈고 글이 없었다면 방·일지에 "무엇을 시도했는지"가
        # 남아야 하니 빈 문자열 대신 이 문구로 채운다(사진 없이 성공했을 때의 문구와는 다르게 둔다).
        q = "(사진 첨부 실패)"
    # 사진 처리 실패는 그 자체가 이미 확정된 오류이므로 질문 길이 검증을 건너뛴다 — 사용자가
    # 글까지 짧게 썼더라도(또는 위에서 채운 대체 문구든) "질문은 2~500자로" 안내로 뭉개지 않고
    # "사진을 읽지 못했습니다" 쪽이 그대로 보여야 실제 원인과 화면 안내가 맞아떨어진다.
    if not photo_error and not (2 <= len(q) <= 500):
        return JSONResponse({"ok": False, "error": "질문은 2~500자로 적어주세요"}, status_code=400)
    if not BRAIN_URL:
        return JSONResponse({"ok": False, "error": "세탁 상담이 아직 준비 중입니다"}, status_code=503)
    con = db()
    if chat_id == 0:
        # 새 방 — 제목은 사람이 따로 안 정해도 되게 첫 질문 앞 30자를 그대로 쓴다
        chat_id = insert_id(con, "INSERT INTO chats (staff_id, title, created_at) VALUES (?,?,?)",
                            (user["id"], q[:30], now()))
    else:
        # 기존 방 — 내 방이 맞는지 먼저 확인한다 (남의 방 번호를 실어 보내는 요청을 막는다)
        chat = con.execute("SELECT staff_id FROM chats WHERE id=?", (chat_id,)).fetchone()
        if chat is None or chat["staff_id"] != user["id"]:
            con.close()
            return JSONResponse({"ok": False, "error": "이 상담방에 접근할 수 없습니다"}, status_code=403)
    # ── 계약 v3 (HANDOFF r5에서 확정) — 질문에 「대화 이력」과 「업무 컨텍스트」를 딸려 보낸다 ──
    # 이력(history): 이 방의 기존 대화 최근 6줄 — "그럼 찬물은?" 같은 후속 질문의 맥락 해석용.
    # ⚠️ 이번 질문을 저장하기 「전」에 모아야 이력에 이번 질문이 중복으로 들어가지 않는다.
    # 실패 안내(ok=0)였던 봇 줄은 뺀다 — "상담원이 쉬고 있습니다"는 대화 맥락이 아니라 소음이다.
    hist_rows = con.execute("""SELECT role, content FROM chat_msgs
        WHERE chat_id=? AND NOT (role='bot' AND ok=0)
        ORDER BY id DESC LIMIT 6""", (chat_id,)).fetchall()
    history = [{"role": r["role"], "content": r["content"][:500]} for r in reversed(hist_rows)]
    # 컨텍스트(context): 업무 시야를 역할대로 자른 미완료 티켓(긴급 먼저, 최대 10개 — 계약 상한).
    # 모델은 업무 질문("오늘 할 일", "지금 처리 중인 게 뭐야")에 이 목록「만」을 근거로 답한다 —
    # DB를 통째로 아는 게 아니라 질문 순간에 그 사람 시야만큼만 딸려 가는 구조
    # (화면의 인가 규칙과 같은 축: 사장=전체, 공장장=자기 공장, 직원·기사=본인 배정. 2026-08-11 확장).
    if user["role"] == "owner":
        where, args = "", ()                                          # 전체
    elif user["role"] == "manager":
        where, args = " AND cl.factory_id=?", (user["factory_id"],)   # 자기 공장
    else:
        where, args = " AND c.assignee_id=?", (user["id"],)           # 본인 배정 (기존 그대로)
    trows = con.execute(TICKET_SELECT + """
        WHERE c.status != 'done'""" + where + """
        ORDER BY (c.severity='urgent') DESC, c.created_at ASC LIMIT 10""", args).fetchall()
    tickets = []
    for t in trows:
        # 티켓마다 최신 지시 한 줄 — 기사가 "뭘 하라고 했는지"까지 답에 실리게
        instr = con.execute("""SELECT content FROM actions
            WHERE complaint_id=? AND kind='instruct' AND deleted_at IS NULL
            ORDER BY id DESC LIMIT 1""",
            (t["id"],)).fetchone()          # 삭제된 지시는 AI 답변 재료에서도 뺀다
        row = {"id": t["id"], "client": t["client_name"],
               "status": STATUS_LABEL[t["status"]] + ("(긴급)" if t["severity"] == "urgent" else ""),
               "content": t["content"][:200],
               "instruction": (instr["content"][:200] if instr else "")}
        if user["role"] in ("owner", "manager"):
            # 사장·공장장의 물음은 "누가 하고 있나"까지다 — 담당자 이름을 계약에 추가 필드로 싣는다
            # (r16 제안: 노트북이 이 필드를 프롬프트에 실어줘야 모델이 이름으로 답할 수 있다.
            #  노트북이 아직 모르는 필드는 무시되므로, 반영 전에도 기존 동작은 깨지지 않는다)
            # 노트북이 이 필드를 정식으로 프롬프트에 싣는다(r17 — "/ 담당: 이름" 렌더링).
            # 반영 전 쓰던 content [담당:] 접두 임시 조치는 r17 신호대로 제거 — 남기면 담당이 두 번 보인다.
            row["assignee"] = t["assignee_name"] or "(미배정)"
        tickets.append(row)
    payload = {"question": q, "history": history,
               "context": {"user": f"{user['name']}({ROLE_LABEL[user['role']]})", "tickets": tickets}}
    if img_b64:
        # 사진은 이번 한 턴의 payload에만 실린다 — history·chat_msgs에는 절대 안 들어간다(r8 §2-4).
        # base64가 이력에 실려 다음 질문마다 계속 되풀이 전송되는 일을 막는 것이 이 분리의 목적이다.
        payload["image"] = img_b64
    # 질문을 먼저 남긴다 — 두뇌 호출이 실패해도 "무엇을 물었는지"는 방에 남아야 한다
    con.execute("INSERT INTO chat_msgs (chat_id, role, content, created_at) VALUES (?,?,?,?)",
                (chat_id, "user", q, now()))
    con.commit()
    if photo_error:
        # 사진 처리 실패 — 두뇌를 부를 것도 없이(어차피 보낼 사진이 없다) 바로 오류 모양을 만든다.
        # 아래 저장·응답 코드는 두뇌 호출 실패 때와 한 글자도 다르지 않은 경로를 그대로 탄다.
        body = {"ok": False, "error": photo_error}
    else:
        try:
            req = urllib.request.Request(BRAIN_URL + "/api/ask",
                                         data=json.dumps(payload, ensure_ascii=False).encode("utf-8"))
            req.add_header("Content-Type", "application/json")
            req.add_header("X-Brain-Token", BRAIN_TOKEN)
            req.add_header("ngrok-skip-browser-warning", "1")   # ngrok 무료 도메인의 브라우저 경고 페이지 우회(r5 권장)
            # 타임아웃 120초 — 노트북 모델이 잠들어 있던 첫 질문은 2분까지 걸릴 수 있다(HANDOFF)
            body = json.loads(urllib.request.urlopen(req, timeout=120).read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read().decode("utf-8"))
            except Exception:
                body = {"ok": False, "error": f"답변기 응답 오류({e.code}) — 잠시 후 다시 시도해 주세요."}
        except Exception:                    # 노트북 꺼짐·터널 끊김 — 사이트는 멀쩡해야 한다
            body = {"ok": False, "error": "지금은 상담원이 쉬고 있습니다. 잠시 후 다시 시도해 주세요."}
    # 상담원(봇) 답을 저장 — sources는 목록이라 칸 하나에 못 담으니 JSON 문자열로 눌러 담는다
    # (근거 목록은 항상 그 답 하나에만 딸려 함께 읽히므로 표를 더 쪼개지 않아도 된다)
    con.execute("""INSERT INTO chat_msgs (chat_id, role, content, sources, ok, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (chat_id, "bot", str(body.get("answer") or body.get("error") or "")[:2000],
                 json.dumps(body.get("sources") or [], ensure_ascii=False),
                 1 if body.get("ok") else 0, now()))
    con.commit(); con.close()
    body["chat_id"] = chat_id   # 새로 만든 방이었다면 화면이 이 번호로 주소를 바꿔 달게 알려준다
    return JSONResponse(body)


@app.get("/kakao/setup")
def kakao_setup(request: Request):
    """카톡 알림 연동 시작(사장 전용) — 카카오 동의 화면으로 보낸다.
    무료 범위(나에게 보내기)라 알림 수신자는 여기서 동의한 본인 계정 하나다."""
    user = require_user(request)
    if user is None or user["role"] != "owner":
        return RedirectResponse("/", status_code=303)
    if not KAKAO_REST_KEY:
        return HTMLResponse("KAKAO_REST_KEY 환경변수가 없습니다 — 카카오 디벨로퍼스 앱의 "
                            "REST API 키를 서버 환경변수로 등록한 뒤 다시 시도하세요.")
    # prompt=login: 브라우저에 남은 카카오 세션을 무시하고 로그인부터 다시 — 다른 계정으로 갈아탈 수 있게
    return RedirectResponse(
        "https://kauth.kakao.com/oauth/authorize?response_type=code"
        f"&client_id={KAKAO_REST_KEY}&redirect_uri={_kakao_redirect_uri(request)}"
        "&scope=talk_message&prompt=login")


@app.get("/kakao/callback")
def kakao_callback(request: Request, code: str = "", error: str = ""):
    """카카오 동의 뒤 돌아오는 곳 — 받은 코드를 토큰으로 바꿔 DB에 저장하고, 확인 카톡을 보낸다."""
    user = require_user(request)
    if user is None or user["role"] != "owner":
        return RedirectResponse("/", status_code=303)
    if error or not code:
        return HTMLResponse(f"카카오 연동 실패: {error or '인가 코드가 없습니다'}")
    tok = _kakao_token_request({"grant_type": "authorization_code", "client_id": KAKAO_REST_KEY,
                                "redirect_uri": _kakao_redirect_uri(request), "code": code})
    if "refresh_token" not in tok:
        # 대표 사례: KOE010 = 앱의 Client Secret이 「사용함」인데 서버에 KAKAO_CLIENT_SECRET이 없음
        return HTMLResponse("카카오 토큰 발급 실패 — 아래 내용을 확인하세요.<br><pre>"
                            + json.dumps(tok, ensure_ascii=False, indent=2) + "</pre>")
    con = db()
    setting_set(con, "kakao_refresh_token", tok["refresh_token"])
    setting_set(con, "kakao_access_token", tok["access_token"])
    setting_set(con, "kakao_access_expires", str(time.time() + tok.get("expires_in", 21600)))
    # 어느 계정에 연동됐는지 저장 — 화면에 표시하기 위해 (기본 동의만으로는 회원번호뿐이라 끝 4자리로)
    try:
        req2 = urllib.request.Request("https://kapi.kakao.com/v2/user/me")
        req2.add_header("Authorization", f"Bearer {tok['access_token']}")
        me = json.loads(urllib.request.urlopen(req2, timeout=10).read().decode("utf-8"))
        nick = (me.get("properties") or {}).get("nickname") or \
               ((me.get("kakao_account") or {}).get("profile") or {}).get("nickname")
        setting_set(con, "kakao_account", (nick + " " if nick else "계정 ") + "…" + str(me.get("id"))[-4:])
    except Exception:
        pass                                     # 식별 실패해도 연동 자체는 유효
    con.commit(); con.close()
    kakao_send_to_me("[세탁 클레임] 카톡 알림 연동 완료 — 새 클레임이 접수되면 이 「나와의 채팅」으로 알림이 옵니다.")
    return RedirectResponse("/", status_code=303)


@app.get("/kakao/test")
def kakao_test(request: Request):
    """테스트 발송(사장 전용) — 어느 계정으로 가는지, 발송이 살아 있는지를 바로 확인하는 버튼."""
    user = require_user(request)
    if user is None or user["role"] != "owner":
        return RedirectResponse("/", status_code=303)
    try:
        sent = kakao_send_to_me("[세탁 클레임] 테스트 발송 — 이 메시지가 보이는 카카오 계정이 알림 수신 계정입니다.")
    except Exception as e:
        return HTMLResponse(f"테스트 발송 실패: {e} <p><a href='/'>← 대시보드로</a></p>")
    if not sent:
        return HTMLResponse("아직 연동되지 않았습니다. <p><a href='/kakao/setup'>연동하러 가기</a></p>")
    return HTMLResponse("테스트 메시지를 보냈습니다 — 카카오톡의 <b>「나와의 채팅」</b>방을 확인하세요. "
                        "(내가 나에게 보낸 메시지는 푸시 알림이 울리지 않습니다)"
                        "<p><a href='/'>← 대시보드로</a></p>")


@app.get("/kakao/disconnect")
def kakao_disconnect(request: Request):
    """연동 해제(사장 전용) — 카카오 쪽 앱 연결도 끊고, 서버에 보관한 토큰을 지운다.
    해제 후 「연동하기」를 다시 누르면 로그인부터 시작하므로 다른 계정으로 갈아탈 수 있다."""
    user = require_user(request)
    if user is None or user["role"] != "owner":
        return RedirectResponse("/", status_code=303)
    try:
        token = kakao_access_token()
        if token:                                # 카카오 계정 쪽의 앱 연결 자체를 해제
            req2 = urllib.request.Request("https://kapi.kakao.com/v1/user/unlink", data=b"")
            req2.add_header("Authorization", f"Bearer {token}")
            urllib.request.urlopen(req2, timeout=10)
    except Exception as e:
        print("카카오 unlink 실패(토큰 삭제는 계속):", e)
    con = db()
    con.execute("DELETE FROM settings WHERE key LIKE 'kakao_%'")
    con.commit(); con.close()
    return RedirectResponse("/", status_code=303)


# ── 데모 데이터 초기화 (2026-08-11) ──────────────────────────────────
# 배포 데모는 면접관·방문자가 시험 삼아 남긴 클레임·조치가 Neon Postgres에 그대로 쌓인다.
# 시연 전에 처음 상태(예시 데이터)로 되돌릴 버튼이 필요해서 만든다. 사장 전용 — 함부로 지우면
# 다른 방문자가 만든 기록이 통째로 사라지므로 role 가드를 GET·POST 둘 다에 건다.
TABLES_TO_WIPE = ("photos", "actions", "complaints", "chat_msgs", "chats",
                  "client_items", "items", "staff", "clients", "factories")
# ⚠️ settings 표(카카오 토큰)는 이 목록에 없다 — 지우면 배포 데모의 카카오 알림 연동이
#    끊겨 버리는데, 그건 이 버튼이 할 일이 아니다(연동 해제는 /kakao/disconnect 몫).


@app.get("/admin/reset")
def admin_reset_form(request: Request):
    """데모 초기화 확인 화면(사장 전용) — 누르면 끝인 버튼이 아니라, 지금 쌓인 규모를 먼저
    보여주고 한 번 더 확인시키는 화면이다(되돌릴 수 없는 작업이라 JS confirm 대신 화면 자체를 관문으로 둔다)."""
    user = require_user(request)
    if user is None or user["role"] != "owner":
        return RedirectResponse("/", status_code=303)
    con = db()
    n_complaints = con.execute("SELECT COUNT(*) FROM complaints").fetchone()[0]
    n_actions = con.execute("SELECT COUNT(*) FROM actions").fetchone()[0]
    n_chats = con.execute("SELECT COUNT(*) FROM chats").fetchone()[0]
    con.close()
    return templates.TemplateResponse(request, "reset.html", {
        "n_complaints": n_complaints, "n_actions": n_actions, "n_chats": n_chats,
    })


@app.post("/admin/reset")
def admin_reset_submit(request: Request):
    """데모 초기화 실행(사장 전용) — 쌓인 데이터를 전부 비우고 최초 부팅 때와 같은 예시 데이터로 되돌린다."""
    user = require_user(request)
    if user is None or user["role"] != "owner":
        return RedirectResponse("/", status_code=303)
    con = db()
    if IS_PG:
        # 자식→부모 순서를 일일이 고민할 필요 없이 한 문장 — RESTART IDENTITY로 자동 id도
        # 다시 1부터 매겨진다. 이게 필요한 이유: seed_demo() 안의 add_ticket()이 "거래처 3번"처럼
        # 자동 생성된 id 순서에 기대어 예시 티켓을 배정하므로, id가 이어서 커지면 배정이 어긋난다.
        con.execute("TRUNCATE " + ", ".join(TABLES_TO_WIPE) + " RESTART IDENTITY CASCADE")
    else:
        # SQLite에는 TRUNCATE가 없다 — 자식 표부터 순서대로 DELETE.
        # SQLite의 id는 INTEGER PRIMARY KEY(AUTOINCREMENT 아님)라, 표를 통째로 비우면
        # 다음 INSERT가 다시 1부터 받는다 — 그래서 위 목록과 같은 자식→부모 순서만 지키면 된다.
        for t in TABLES_TO_WIPE:
            con.execute(f"DELETE FROM {t}")
    seed_demo(con)              # 최초 부팅 때와 같은 함수 — 예시 데이터를 다시 심는다
    con.commit(); con.close()
    # staff도 지웠다 다시 심지만, 위 초기화로 id가 지우기 전과 같은 순서(사장=1, 강만석=2, …)로
    # 다시 매겨지므로 로그인 쿠키(uid)가 가리키던 번호가 여전히 같은 사람을 가리킨다 —
    # 초기화 뒤에도 다시 로그인할 필요가 없다.
    # 업로드 폴더 정리: 방문자가 올린 사진만 지운다. 데모 사진(make_demo_photos.py 생성분)은
    # 전부 "demo_" 접두사라 이름으로 구분된다 — 그 사진들은 예시 티켓이 다시 가리키므로 남겨야 한다.
    for f in UPLOAD_DIR.iterdir():
        if f.is_file() and not f.name.startswith("demo_"):
            f.unlink()
    return RedirectResponse("/?reset=1", status_code=303)
