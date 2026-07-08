-- v2 마이그레이션: web_search_tool 도입에 따른 external_references 컬럼 추가
-- 이미 schema.sql로 테이블을 만들어 로그가 쌓인 환경에서는, 전체 재생성(schema.sql 재실행) 대신
-- 이 스크립트만 실행할 것 — DROP TABLE이 없어 기존 agent_audit_log 데이터가 보존됨.
--
-- 실행: psql -h localhost -U claimprecedent -d claimprecedent -f migrate_v3_external_references.sql

ALTER TABLE agent_audit_log
    ADD COLUMN IF NOT EXISTS external_references JSONB DEFAULT '[]'::jsonb;
