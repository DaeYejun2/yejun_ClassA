# -*- coding: utf-8 -*-
"""
ClaimPrecedent CLI 데모 — 설계보고서 9.1 시나리오 재현
실제 OpenAI API(임베딩+LLM) 및 PostgreSQL(pgvector)에 연결해서 동작합니다.
OCI VM 등 아웃바운드 제한 없는 환경에서 실행하세요.

사용법:
    python3 demo.py                 # report 9.1 기본 시나리오
    python3 demo.py "직접 입력할 청구 건 텍스트"
"""
import sys
from agent import build_graph
from langgraph.types import Command

DEFAULT_QUERY = (
    "실손보험 가입자가 A 시술을 받고 진료비 청구. 치료 목적과 미용 목적이 혼재되어 보이는데, "
    "유사 분쟁조정사례가 있는지 확인해줘."
)


def print_section(title: str):
    print(f"\n{'─' * 50}\n{title}\n{'─' * 50}")


def run_demo(query: str):
    app = build_graph()
    config = {"configurable": {"thread_id": "cli-demo"}}

    print_section("① 심사역 입력")
    print(query)

    result = app.invoke(
        {"input_query": query, "product_type": "실손의료보험"},
        config=config,
    )

    print_section("② Agent가 자율적으로 호출한 Tool 이력 (ReAct)")
    for i, t in enumerate(result.get("tool_call_trace", []), 1):
        # 주의: 이전 버전은 리스트 인자를 v[:3]으로 잘라서 보여줬는데, 실제 tool에는 전체가
        # 넘어가기 때문에 표시만 잘려서 착시를 유발할 수 있었음(예: candidate_case_ids가
        # 실제로는 4개인데 3개만 보여서 "reranker가 후보 밖 사례를 반환했다"고 오인). 전체 출력으로 수정.
        print(f"  [{i}] {t['tool']}({t['arguments']})")
        print(f"       -> 반환값: {t['result']}")
    print()
    print(f"추출된 쟁점: {result['extracted_issue']}")
    print(f"검색/인용된 최종 사례: {[c['case_id'] for c in result.get('filtered_cases', [])]}")
    if result.get("external_references"):
        print("외부참고자료(웹검색, 근거강도 미반영):")
        for ref in result["external_references"]:
            print(f"  - {ref.get('title')} ({ref.get('url')})")

    if "__interrupt__" in result:
        payload = result["__interrupt__"][0].value
        print_section("③ HITL 승인 게이트 발동")
        print(f"사유: {payload['hitl_reason']}")
        print(f"근거강도: {payload['evidence_strength']}")
        print(f"참고의견(초안): {payload['draft_opinion']}")
        print(f"AI 추천: {payload['recommendation']}")
        if payload.get("external_references"):
            print("외부참고자료(웹검색):")
            for ref in payload["external_references"]:
                print(f"  - {ref.get('title')} ({ref.get('url')})")
        print()
        print("⚠ 위 내용은 참고자료이며 최종 결정이 아닙니다. 심사역의 승인이 필요합니다.")
        decision = input("\n심사역 결정 입력 [ACCEPT/MODIFY/REJECT] (기본값 ACCEPT): ").strip() or "ACCEPT"
        note = input("코멘트(선택, 엔터로 생략): ").strip()

        result = app.invoke(
            Command(resume={"decision": decision, "note": note}),
            config=config,
        )
        print_section("④ 심사역 최종 결정 반영 완료")
    else:
        print_section("③ 저위험/근거충분 → HITL 없이 참고의견 바로 제시")

    print_section("⑤ 최종 결과 (감사 로그 저장됨)")
    print(f"근거강도: {result['evidence_strength']}")
    print(f"AI 참고의견: {result['draft_opinion']}")
    print(f"AI 추천: {result['recommendation']}")
    print(f"HITL 필요 여부: {result['hitl_required']} ({result.get('hitl_reason') or '해당없음'})")
    print(f"심사역 최종결정: {result.get('human_decision')}")
    if result.get("human_note"):
        print(f"심사역 코멘트: {result['human_note']}")


if __name__ == "__main__":
    query = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else DEFAULT_QUERY
    run_demo(query)
