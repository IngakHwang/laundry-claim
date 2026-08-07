# 🔄 BATON — laundry-ticket (배포명 laundry-claim)

> 🔗 데모 https://laundry-claim.onrender.com · 저장소 github.com/IngakHwang/laundry-claim (공개)
> 로컬 폴더명은 laundry-ticket, 원격·배포명은 laundry-claim — 이름이 다른 건 알고 둔 것.

## 🎯 다음 할 일
- **이력서·포트폴리오에 링크 반영** — 원티드 이력서 링크 칸(미판정 상태였음)에 데모 주소 + GitHub. 라이넨스 지원 준비 세션과 겹치니 그쪽에서 다뤄도 됨
- GitHub 저장소 About 란에 데모 링크·설명 달기 (repo 첫인상)
- README에 화면 캡처 1~2장 (지금은 글만 — 캡처는 인각님 브라우저에서)
- **v2 후보(순서 미정):** ①SQLite→Postgres 이관(데이터 영속 — 다음 경험 카드) ②비밀번호 인증 ③SMS(notify() 자리)

## ❓ 인각님이 답할 것
- (지금은 없음)

## ⚠️ 주의
- **무료 서버는 15분 쉬면 잠듦** — 면접·시연 전에 미리 한 번 열어 깨워둘 것 (깨는 데 1분)
- **서버 재시작 = 데이터 초기화**(디스크 비영속) — 데모엔 장점, 실데이터 쓰려면 Postgres 이관 먼저 (2026-08-07)
- 범위 관리: 진짜 인증·SMS는 v2 (2026-08-07)
