# 피지컬AI·AI Agent 설계 및 활용

# ClaimPrecedent

보험 지급심사 담당자를 위한 유사 분쟁사례 기반 지급판단 보조 AI Agent

> 계절학기 과제 — 관심 산업 기반 AI Agent 도입 전략 및 Delivery 설계
> 2023086025 문예준

## 개요

보장범위 해석이 애매한 보험금 청구 건에 대해, 금융감독원 분쟁조정사례 코퍼스(201건, 보험 권역 160건)에서 유사 판례를 검색·재검증하여 심사역에게 판단 근거를 제시하는 Agent입니다. Agent는 참고자료 제공까지만 수행하며, 최종 지급 결정은 항상 사람(심사역)에게 남습니다.

- **구조**: LangGraph StateGraph — 자율 Tool-calling ReAct 루프(react) + 결정론적 가드레일(risk → hitl → log)
- **핵심 통제**: CRAG 관련성 재검증, case_id 서버 측 이중 검증(환각 방지), 조건부 HITL 승인 게이트, 감사로그
- **스택**: gpt-4o-mini(function-calling) · text-embedding-3-small · PostgreSQL 16 + pgvector · LangGraph

## 사용 모델 및 비용 산출 기준

LLM 호출은 전 구간 **gpt-4o-mini 단일 모델**입니다 (react 드라이버와 relevance_reranker 내부 관련성 판정이 같은 모델을 별도 호출).

| 모델 | 사용처 | 단가 (OpenAI 공시, 작성 시점) |
|---|---|---|
| gpt-4o-mini (function-calling) | react 루프 드라이버 — 쟁점 이해, tool 호출 판단, 참고의견 생성 | 입력 $0.15 / 출력 $0.60 (1M tokens) |
| gpt-4o-mini | relevance_reranker 내부의 관련성(CRAG) 판정 | 상동 |
| text-embedding-3-small | 코퍼스 적재 시 사례 벡터화 + 심사역 질의 실시간 벡터화 | $0.02 (1M tokens) |

케이스당 비용 산출식과 실측 검산:

```
비용 = (입력토큰 × $0.15 + 출력토큰 × $0.60 + 임베딩토큰 × $0.02) ÷ 1,000,000
실측 평균: 10,676 × 0.15 + 387 × 0.60 + 40 × 0.02 ≈ $0.0018 / 케이스
```

gpt-4o-mini를 선택한 이유: 산출물이 참고자료이고 HITL이 최종 관문이므로 초안 품질 요구 수준 대비 케이스당 비용·지연 최소화를 우선했습니다. 더 큰 모델과의 A/B(리랭커 잔여 오류가 모델 추론력 한계인지 검증 포함)는 향후 과제입니다.

## Agent 자율성 범위 — 혼자 하는 것과 못 하는 것

| 구분 | 내용 |
|---|---|
| LLM이 스스로 결정 (react 루프 내) | 검색어 구성·재구성, tool 호출 순서·횟수(최대 8회), 후보 사례 선택, finalize 시점, 참고의견·근거강도·추천 **초안** |
| LLM 재량 밖 (결정론적 강제) | 인용 사례 최종 확정(case_id_validator 서버 측 이중검증), risk 판정, HITL 발동 여부, 조기 기권 규칙, 감사로그 저장 |
| 사람 없이 end-to-end 완료 | 근거강도 HIGH + 추천 APPROVE + 웹검색 미사용의 교집합뿐 — 실측 40회 중 자동 통과 1건(2.5%) |

즉 Agent가 혼자 완료하는 범위는 의도적으로 좁게 설계되어 있으며(자동 통과율 2.5%), 자율성은 근거 **탐색**에만 부여되고 **결정**은 규칙과 사람에게 남습니다.

## 제출물 위치

| 제출물 | 파일 |
|---|---|
| 최종 보고서 | `AI_Agent_설계_및_활용_최종.docx` |
| 발표자료 | `발표자료_ClaimPrecedent.pptx` |
| AI Agent 설계도 | `업무 흐름도 및 아키텍처 다이어그램.pdf` |
| 프로토타입 / 데모 시나리오 | `src/` 전체 코드 + `ClaimPrecedent 데모 시나리오.docx` |
| 참고문헌 및 출처 | `참고문헌 및 출처 목록.docx` |

## 저장소 구성

```
├── README.md
├── .gitignore / .env.example
├── AI_Agent_설계_및_활용_최종.docx
├── 발표자료_ClaimPrecedent.pptx
├── docs/
│   ├── 설계도_1_업무흐름도.png
│   ├── 설계도_2_시스템아키텍처.png
│   └── 데모_시나리오.md
├── requirements.txt
├── data/
│   ├── fss_dispute_cases.csv             # 금감원 분쟁조정사례 201건 (보험 권역 160건) — 크롤링 원천 데이터
│   └── cleaned_cases.csv                 # 전처리 결과 (섹션 파싱·노이즈 제거, 임베딩용 case_text 포함)
└── src/
    ├── clean_data.py                     # 원천 CSV 전처리 (섹션 파싱, 텍스트 정규화)
    ├── embed_and_load.py                 # cleaned_cases.csv 임베딩(text-embedding-3-small) -> pgvector 적재
    ├── agent.py                          # LangGraph Agent 본체 (react/risk/hitl/log)
    ├── demo.py                           # 단건 CLI 데모
    ├── batch_eval.py                     # 평가지표 실측용 배치 실행기 (질의 15건 내장)
    ├── schema.sql                        # pgvector 코퍼스 + 감사로그 테이블
    ├── migrate_v3_external_references.sql
    ├── migrate_v4_metrics_columns.sql    # 평가지표 실측용 컬럼
    └── metrics.sql                       # 12장 평가지표 6종 집계 쿼리
```

## 실행 방법

전제: PostgreSQL 16 + pgvector, Python 3.9+, 아웃바운드 가능 환경(OCI VM 등)

```bash
# 1. 의존성
pip install -r requirements.txt

# 2. 환경변수 — .env.example을 복사해 본인 키를 채움
cp .env.example .env

# 3. DB·계정 생성 (최초 1회) 후 스키마 생성 (schema.sql은 v4 컬럼까지 포함된 최신본)
sudo -u postgres psql -c "CREATE USER claimprecedent WITH PASSWORD '비밀번호';"
sudo -u postgres psql -c "CREATE DATABASE claimprecedent OWNER claimprecedent;"
sudo -u postgres psql -d claimprecedent -c "CREATE EXTENSION IF NOT EXISTS vector;"
psql -h localhost -U claimprecedent -d claimprecedent -f src/schema.sql

# 4. 코퍼스 적재 — 전처리된 data/cleaned_cases.csv를 임베딩해 DB에 적재
python3 src/embed_and_load.py
#    (원천 데이터부터 재현하려면 src/clean_data.py로 전처리부터 수행 가능)

# 5. 단건 데모
python3 src/demo.py
python3 src/demo.py "직접 입력할 청구 건 텍스트"

# 6. 평가지표 실측 (배치 15건 → SQL 집계)
python3 src/batch_eval.py                   # HITL 수동 판정 (override 지표 측정용)
python3 src/batch_eval.py --auto-accept     # 전부 자동 승인 (latency/cost만 볼 때)
psql -h localhost -U claimprecedent -d claimprecedent -f src/metrics.sql
```

## 실측 결과 요약 (총 40회 실행, 자체 테스트 기준)

정확도(Precision) 0.80 · 환각 인용 0% · Task 달성률 82.6%(미완료는 전부 코퍼스 무관 쟁점의 기권) · 평균 latency 10.7초 · 케이스당 비용 약 $0.0018(gpt-4o-mini + text-embedding-3-small, 산출식은 위 '사용 모델 및 비용 산출 기준' 참조) · HITL 개입률 97.5%(자동 통과 1건 = 2.5%) · Human override 7.5%
상세 정의·해석은 보고서 12장, 대표 실행 사례 3종(정상 인용 / 오류 방어 / 정직한 기권)은 `ClaimPrecedent 데모 시나리오.docx` 참조.

## 데이터 출처

`data/fss_dispute_cases.csv`는 금융감독원 통합 홈페이지 분쟁조정사례 게시판(fss.or.kr)에서 크롤링한 공개 자료입니다. 금감원이 익명 처리하여 공개한 사례로 개인 식별 정보를 포함하지 않으며, 본 과제의 학술 목적 범위에서 사용합니다.
