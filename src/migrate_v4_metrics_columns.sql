
-- v4 마이그레이션: 12장 평가지표(환각율/task달성률/latency/token cost) 실측을 위한 컬럼 추가
-- 이미 schema.sql(v3까지 반영본)로 테이블을 만들어 로그가 쌓인 환경에서는, 전체 재생성
-- (schema.sql 재실행) 대신 이 스크립트만 실행할 것 — DROP TABLE이 없어 기존
-- agent_audit_log 데이터가 보존됨.
--
-- 실행: psql -h localhost -U claimprecedent -d claimprecedent -f migrate_v4_metrics_columns.sql

ALTER TABLE agent_audit_log
    ADD COLUMN IF NOT EXISTS loop_completed BOOLEAN,
    ADD COLUMN IF NOT EXISTS hallucinated_case_ids TEXT[],
    ADD COLUMN IF NOT EXISTS not_reranked_case_ids TEXT[],
    ADD COLUMN IF NOT EXISTS cited_case_count INTEGER,
    ADD COLUMN IF NOT EXISTS first_response_latency_sec NUMERIC,
    ADD COLUMN IF NOT EXISTS react_latency_sec NUMERIC,
    ADD COLUMN IF NOT EXISTS chat_input_tokens INTEGER,
    ADD COLUMN IF NOT EXISTS chat_output_tokens INTEGER,
    ADD COLUMN IF NOT EXISTS embedding_tokens INTEGER,
    ADD COLUMN IF NOT EXISTS estimated_cost_usd NUMERIC;
