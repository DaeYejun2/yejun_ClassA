-- ============================================================
-- ClaimPrecedent 12장 평가지표 집계 쿼리
-- 실행: psql -h localhost -U claimprecedent -d claimprecedent -f metrics.sql
-- 전제: agent_audit_log에 v4 컬럼까지 반영 + 배치 테스트 로그가 쌓여 있을 것
-- ============================================================

\echo '=== ① 전체 실행 건수 ==='
SELECT count(*) AS total_runs FROM agent_audit_log;

\echo '=== ② 환각 rate (인용 시도 대비 validator 반려 비율) ==='
SELECT
    sum(cited_case_count)                                            AS cited_total,
    sum(coalesce(array_length(hallucinated_case_ids, 1), 0))         AS hallucinated_total,
    sum(coalesce(array_length(not_reranked_case_ids, 1), 0))         AS crag_bypass_total,
    round(100.0 * sum(coalesce(array_length(hallucinated_case_ids, 1), 0))
        / nullif(sum(cited_case_count), 0), 2)                       AS hallucination_rate_pct,
    round(100.0 * (sum(coalesce(array_length(hallucinated_case_ids, 1), 0))
                 + sum(coalesce(array_length(not_reranked_case_ids, 1), 0)))
        / nullif(sum(cited_case_count), 0), 2)                       AS invalid_citation_rate_pct
FROM agent_audit_log;

\echo '=== ③ Task 달성률 (MAX_ITERATIONS 내 finalize_opinion 정상 종료 비율) ==='
SELECT
    count(*) FILTER (WHERE loop_completed)                           AS completed,
    count(*) FILTER (WHERE loop_completed IS NOT NULL)               AS measured,
    round(100.0 * count(*) FILTER (WHERE loop_completed)
        / nullif(count(*) FILTER (WHERE loop_completed IS NOT NULL), 0), 2)
                                                                     AS task_completion_pct
FROM agent_audit_log;

\echo '=== ④ Avg latency (첫 응답 / react 루프 전체, 초. HITL 대기시간 미포함) ==='
SELECT
    round(avg(first_response_latency_sec), 2)                        AS avg_first_response_sec,
    round(avg(react_latency_sec), 2)                                 AS avg_total_react_sec,
    round(percentile_cont(0.5) WITHIN GROUP (ORDER BY react_latency_sec)::numeric, 2)
                                                                     AS p50_react_sec,
    round(percentile_cont(0.95) WITHIN GROUP (ORDER BY react_latency_sec)::numeric, 2)
                                                                     AS p95_react_sec
FROM agent_audit_log
WHERE react_latency_sec IS NOT NULL;

\echo '=== ⑤ Token cost (케이스당 평균 토큰 및 비용, gpt-4o-mini + text-embedding-3-small 단가) ==='
SELECT
    round(avg(chat_input_tokens))                                    AS avg_chat_input_tokens,
    round(avg(chat_output_tokens))                                   AS avg_chat_output_tokens,
    round(avg(embedding_tokens))                                     AS avg_embedding_tokens,
    round(avg(estimated_cost_usd)::numeric, 6)                       AS avg_cost_usd_per_case,
    round(sum(estimated_cost_usd)::numeric, 4)                       AS total_cost_usd
FROM agent_audit_log
WHERE estimated_cost_usd IS NOT NULL;

\echo '=== ⑥ Human override (HITL 발동 건 중 MODIFY/REJECT 비율) ==='
SELECT
    count(*) FILTER (WHERE hitl_required)                            AS hitl_triggered,
    round(100.0 * count(*) FILTER (WHERE hitl_required) / nullif(count(*), 0), 2)
                                                                     AS hitl_rate_pct,
    count(*) FILTER (WHERE human_decision = 'ACCEPT')                AS accepted,
    count(*) FILTER (WHERE human_decision IN ('MODIFY', 'REJECT'))   AS overridden,
    round(100.0 * count(*) FILTER (WHERE human_decision IN ('MODIFY', 'REJECT'))
        / nullif(count(*) FILTER (WHERE human_decision IS NOT NULL), 0), 2)
                                                                     AS human_override_pct
FROM agent_audit_log;

\echo '=== (보조) 정확도 근사치용 — 수동 판정 대상 케이스 목록 ==='
-- 아래 결과를 보고 각 건의 filtered_case_ids가 쟁점과 실제 관련 있는지 수동 판정하여
-- Precision@k(표본 수동 평가)를 산출. 정식 골드셋 기반 정확도는 향후 과제로 표기.
SELECT log_id, input_query, extracted_issue, filtered_case_ids, evidence_strength, recommendation
FROM agent_audit_log
ORDER BY log_id;
