-- ClaimPrecedent 프로토타입 스키마
-- report 6.5~6.8 기준: fss_dispute_cases 테이블 + 감사 로그 테이블

CREATE EXTENSION IF NOT EXISTS vector;

DROP TABLE IF EXISTS fss_dispute_cases CASCADE;
CREATE TABLE fss_dispute_cases (
    case_id         TEXT PRIMARY KEY,       -- 예: FSS-0201
    domain          TEXT NOT NULL,          -- 권역 (보험/은행중소서민/금융투자)
    case_type       TEXT NOT NULL,          -- 유형 (예: 실손보험(치료비))
    title           TEXT NOT NULL,          -- 제목
    reg_date        TEXT,                   -- 등록일
    complaint       TEXT,                   -- 민원내용
    issue           TEXT,                   -- 쟁점
    resolution      TEXT,                   -- 처리결과
    consumer_note   TEXT,                   -- 소비자유의사항
    case_text       TEXT NOT NULL,          -- 임베딩에 사용한 최종 조립 텍스트
    source_url      TEXT,
    embedding       vector(1536)            -- text-embedding-3-small 차원
);

-- 보험(권역) 케이스만 우선 검색 대상으로 삼는 부분 인덱스도 고려 가능하나,
-- MVP는 전체 코퍼스 대상 HNSW 인덱스로 단순화
CREATE INDEX ON fss_dispute_cases USING hnsw (embedding vector_cosine_ops);

-- 감사 로그 테이블 (report 6.6 audit_logger / 7장 통제방안 대응)
DROP TABLE IF EXISTS agent_audit_log CASCADE;
CREATE TABLE agent_audit_log (
    log_id          SERIAL PRIMARY KEY,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    input_query     TEXT NOT NULL,
    extracted_issue TEXT,
    retrieved_case_ids TEXT[],              -- Act 단계에서 검색된 사례 ID 목록
    filtered_case_ids  TEXT[],              -- Observe(CRAG) 이후 관련성 통과한 사례 ID 목록
    evidence_strength TEXT,                 -- LOW / MEDIUM / HIGH
    recommendation  TEXT,                   -- APPROVE / DENY / ADDITIONAL_REVIEW_NEEDED
    hitl_required   BOOLEAN NOT NULL DEFAULT false,
    hitl_reason     TEXT,
    human_decision  TEXT,                   -- ACCEPT / MODIFY / REJECT (HITL 이후 기록)
    human_note      TEXT,
    external_references JSONB DEFAULT '[]'::jsonb, -- 웹검색으로 얻은 외부참고자료 (v3, 근거강도 미반영)

    -- v4: 12장 평가지표(환각율/task달성률/latency/token cost) 실측용 컬럼
    loop_completed          BOOLEAN,           -- MAX_ITERATIONS 내에 finalize_opinion으로 정상 종료했는지
    hallucinated_case_ids   TEXT[],            -- case_id_validator가 반려한 환각(미검색) case_id
    not_reranked_case_ids   TEXT[],            -- case_id_validator가 반려한 CRAG 미통과 case_id
    cited_case_count        INTEGER,           -- finalize_opinion이 인용 시도한 전체 case_id 개수
    first_response_latency_sec NUMERIC,        -- react 루프 첫 LLM 응답까지 걸린 시간(초)
    react_latency_sec       NUMERIC,           -- react 루프 전체 처리 시간(초). HITL 대기시간은 미포함
    chat_input_tokens       INTEGER,           -- react+reranker LLM 호출 입력 토큰 합계
    chat_output_tokens      INTEGER,           -- react+reranker LLM 호출 출력 토큰 합계
    embedding_tokens        INTEGER,           -- vector_search_tool 임베딩 호출 토큰 합계
    estimated_cost_usd      NUMERIC            -- 케이스당 예상 비용(USD, gpt-4o-mini+embedding 단가 기준)
);
