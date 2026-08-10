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

import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

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
KIND_LABEL = {"register": "접수", "instruct": "지시", "ack": "확인",
              "work": "처리 보고", "done": "완료 처리", "note": "메모"}
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

    def execute(self, sql, params=()):
        return self._con.execute(sql.replace("?", "%s"), params)

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
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")   # 없는 거래처·담당자를 가리키는 데이터가 못 들어오게
    return con


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


def notify(target: str, message: str) -> None:
    """(v2 자리) 문자·알림 발송 — 지금은 아무것도 안 보낸다.
    v2에서 SMS API(유료·발신번호 등록 필요)를 여기 끼우면 나머지 코드는 그대로다."""
    pass


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
    CREATE TABLE IF NOT EXISTS actions (      -- 조치 일지 — append-only, 티켓의 타임라인
        id           {auto_id},
        complaint_id INTEGER NOT NULL REFERENCES complaints(id),
        actor        TEXT NOT NULL,           -- 누가 (이름 글자 — v1은 로그인이 없으므로)
        kind         TEXT NOT NULL CHECK (kind IN ('register','instruct','ack','work','done','note')),
        content      TEXT DEFAULT '',
        created_at   TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS photos (       -- 첨부 사진 — 조치 하나에 여러 장(1:N)이라 표를 따로 둔다
        id        {auto_id},                  -- (한 칸에 파일명 여러 개를 욱여넣으면 정규화 위반)
        action_id INTEGER NOT NULL REFERENCES actions(id),
        filename  TEXT NOT NULL               -- uploads\\ 안의 파일명
    );
    """
    con = db()
    if IS_PG:
        # psycopg는 여러 문장을 한 번에 못 보내므로 문장(;) 단위로 나눠 실행
        for stmt in ddl.split(";"):
            if stmt.strip():
                con.execute(stmt)
    else:
        con.executescript(ddl)
        # 이관: 예전 판은 actions.photo 한 칸에 사진 하나였다 → photos 표로 옮기고 칸을 없앤다
        # (로컬 SQLite 파일에만 있을 수 있는 과거 흔적이라 SQLite에서만 확인)
        old_cols = [row[1] for row in con.execute("PRAGMA table_info(actions)")]
        if "photo" in old_cols:
            con.execute("""INSERT INTO photos (action_id, filename)
                           SELECT id, photo FROM actions WHERE photo != ''""")
            con.execute("ALTER TABLE actions DROP COLUMN photo")
    # 비어 있을 때만 예시 데이터 (시연용 — 실제 도입 시 지우면 됨)
    # ⚠️ 업체 이름은 전부 가상이다. 실존 호텔·모텔의 「유형과 규모」만 본떴다 —
    #    실명에 지어낸 컴플레인을 붙여 공개 배포하면 그 업체에 대한 허위 기록이 되기 때문.
    if con.execute("SELECT COUNT(*) FROM clients").fetchone()[0] == 0:
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


# 사이드바가 어느 화면에서든 부르는 전역 함수들
templates.env.globals["recent_tickets"] = recent_tickets
templates.env.globals["get_user"] = require_user


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
    # 최근 완료는 현황판의 완료 열과 같은 상한(8건) — 한 화면에서 완료 개수가
    # 7·8·5로 제각각 보이던 문제를 기준 통일로 없앤다 (2026-08-10 UI 점검 지적)
    done_recent = con.execute(TICKET_SELECT + " WHERE c.status = 'done'" + fwhere + """
        ORDER BY c.done_at DESC LIMIT 8""", fargs).fetchall()
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
    con.close()
    cols, days_old, photo_n = board_data(factory)   # 대시보드 상단의 칸반 (같은 공장 필터로)
    return templates.TemplateResponse(request, "dashboard.html", {
        "open_tickets": open_tickets, "done_recent": done_recent,
        "cols": cols, "days_old": days_old, "photo_n": photo_n,
        "stats": stats, "by_type": by_type,
        "factories": factories, "cur_factory": factory,
        "allow_all": scope_of(user) is None,   # 본사만 공장 탭을 본다
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
    # 상태별로 묶고, 완료 열은 최근 8건만 (완료가 쌓이면 열이 무한히 길어지므로)
    cols = {s: [] for s in ("new", "acked", "working", "done")}
    today = datetime.now(KST).replace(tzinfo=None)   # created_at(한국 시간 문자열)과 같은 기준으로 경과일 계산
    days_old = {}
    for t in rows:
        cols[t["status"]].append(t)
        created = datetime.strptime(t["created_at"], "%Y-%m-%d %H:%M")
        days_old[t["id"]] = (today - created).days
    cols["done"] = sorted(cols["done"], key=lambda t: t["done_at"] or "", reverse=True)[:8]
    return cols, days_old, photo_n


@app.get("/board")
def board(request: Request):
    """칸반 보드 — 상태별 열(확인 대기→확인됨→처리중→완료)에 티켓이 카드로 붙는다.
    지라(Jira)식 시각화: 일의 흐름이 왼쪽에서 오른쪽으로 흘러가는 게 한눈에 보인다."""
    user = require_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    cols, days_old, photo_n = board_data(scope_of(user))
    return templates.TemplateResponse(request, "board.html", {
        "cols": cols, "days_old": days_old, "photo_n": photo_n,
        "TYPE_LABEL": TYPE_LABEL, "STATUS_LABEL": STATUS_LABEL,
    })


@app.post("/c/{ticket_id}/status")
def set_status(request: Request, ticket_id: int, status: str = Form(...)):
    """보드에서 카드를 끌어다 놓으면 상태를 바꾼다 — 조치 일지에도 한 줄 남긴다(추적 원칙)."""
    user = require_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if status not in STATUS_LABEL:
        return RedirectResponse("/board", status_code=303)
    kind = {"acked": "ack", "working": "work", "done": "done"}.get(status, "note")
    con = db()
    con.execute("INSERT INTO actions (complaint_id, actor, kind, content, created_at) VALUES (?,?,?,?,?)",
                (ticket_id, user["name"], kind, f"칸반 보드에서 「{STATUS_LABEL[status]}」로 이동", now()))
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
    # 인력 명단 (공장장 → 기사 → 인력 순) + 각자의 미완료 배정 건수
    staff = con.execute("""SELECT * FROM staff WHERE factory_id=?
        ORDER BY CASE role WHEN 'manager' THEN 0 WHEN 'driver' THEN 1 ELSE 2 END, name""",
        (factory_id,)).fetchall()
    open_by_staff = dict(con.execute("""SELECT assignee_id, COUNT(*) FROM complaints
        WHERE status != 'done' AND assignee_id IS NOT NULL GROUP BY assignee_id""").fetchall())
    con.close()
    return templates.TemplateResponse(request, "factory.html", {
        "f": factory, "client_rows": client_rows, "total_kg": total_kg,
        "open_tickets": open_tickets, "staff": staff, "open_by_staff": open_by_staff,
        "TYPE_LABEL": TYPE_LABEL, "STATUS_LABEL": STATUS_LABEL, "ROLE_LABEL": ROLE_LABEL,
    })


@app.get("/new")
def new_form(request: Request):
    """컴플레인 접수 폼 — 거래처·담당자 목록을 공장별로 묶어, 자기 공장 범위 안에서만."""
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
        "clients": clients, "staff": staff,
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
    notify(assignee["name"], f"새 컴플레인 #{ticket_id}")   # v2에서 SMS가 될 자리
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
    staff = con.execute("SELECT * FROM staff ORDER BY role, name").fetchall()
    con.close()
    return templates.TemplateResponse(request, "ticket.html", {
        "t": ticket, "acts": acts, "staff": staff, "photo_map": photo_map,
        "TYPE_LABEL": TYPE_LABEL, "STATUS_LABEL": STATUS_LABEL, "KIND_LABEL": KIND_LABEL,
    })


# 조치 종류 → 티켓 상태 자동 변경 규칙 (확인하면 '확인됨', 처리 보고하면 '처리중'…)
KIND_TO_STATUS = {"ack": "acked", "work": "working", "done": "done"}


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
            ORDER BY created_at""", (t["id"],)).fetchall()
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
