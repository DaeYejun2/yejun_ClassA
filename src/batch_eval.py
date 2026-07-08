# -*- coding: utf-8 -*-
"""
ClaimPrecedent 배치 평가 실행기 — 12장 평가지표 실측용
테스트 질의를 순차 실행해 agent_audit_log에 로그를 쌓는다. 이후 metrics.sql로 집계.

사용법:
    python3 batch_eval.py                     # 내장 15건, HITL은 수동 판정(override 측정용)
    python3 batch_eval.py --auto-accept       # HITL 전부 ACCEPT 자동 통과 (latency/cost만 볼 때)
    python3 batch_eval.py --queries my.txt    # 파일에서 질의 로드 (한 줄당 1건)

주의: --auto-accept로 돌린 로그는 human override 지표 계산에서 의미가 없으므로,
override 측정용 실행은 반드시 수동 모드로 할 것.
"""
import argparse
import sys
import time

from agent import build_graph
from langgraph.types import Command

# 쟁점 유형을 섞은 기본 테스트 질의 15건 (실손/후유장해/면책/고지의무 등)
DEFAULT_QUERIES = [
    "실손보험 가입자가 도수치료를 장기간 반복해서 받았는데 보험사가 치료 목적이 아니라며 지급을 거절함. 유사 사례 확인 요청.",
    "백내장 수술 후 다초점 인공수정체 비용을 실손으로 청구했는데 보험사가 시력교정 목적이라며 거절. 판례 확인.",
    "미용 목적과 치료 목적이 혼재된 피부 시술의 진료비 실손 청구 건. 보장범위 해석 애매.",
    "교통사고 후유장해 등급 판정에 대해 계약자와 보험사의 의견이 다름. 후유장해 등급 관련 분쟁 사례 확인.",
    "암 진단비 청구인데 조직검사 결과와 임상 진단이 달라 지급 여부 판단이 애매함.",
    "고지의무 위반을 이유로 계약 해지 및 보험금 부지급 통보를 받은 건. 고지의무 관련 조정 사례 확인.",
    "음주운전 면책 조항 적용 여부가 쟁점인 상해보험 청구 건.",
    "입원 필요성이 인정되는지가 쟁점인 장기 입원 치료비 청구 건. 과잉입원 관련 사례 확인.",
    "신의료기술로 고시되기 전 시행된 시술의 실손 보장 여부가 쟁점.",
    "치아 임플란트 시술이 상해로 인한 치료인지 여부가 쟁점인 청구 건.",
    "정신질환 통원치료비의 실손 보장 여부 관련 분쟁 사례 확인.",
    "보험계약 부활 이후 발생한 사고에 대한 보장 개시 시점이 쟁점인 건.",
    "자살면책 기간 경과 후 사망 사건의 재해사망보험금 지급 여부 쟁점.",
    "수술의 정의(절단·절제 등)에 해당하는지가 쟁점인 수술비 청구 건.",
    "태아보험 가입 후 선천성 질환 진단 건의 보장 여부 쟁점.",
]


def run_one(app, idx: int, query: str, auto_accept: bool):
    config = {"configurable": {"thread_id": f"batch-{idx}-{int(time.time())}"}}
    print(f"\n[{idx}] {query[:60]}...")
    result = app.invoke({"input_query": query, "product_type": "실손의료보험"}, config=config)

    if "__interrupt__" in result:
        payload = result["__interrupt__"][0].value
        print(f"    HITL 발동 — 사유: {payload['hitl_reason']}")
        print(f"    근거강도: {payload['evidence_strength']} / 추천: {payload['recommendation']}")
        print(f"    참고의견: {payload['draft_opinion'][:120]}...")
        if auto_accept:
            decision, note = "ACCEPT", "batch auto-accept"
        else:
            decision = input("    결정 [ACCEPT/MODIFY/REJECT] (기본 ACCEPT): ").strip().upper() or "ACCEPT"
            note = input("    코멘트(엔터로 생략): ").strip()
        result = app.invoke(Command(resume={"decision": decision, "note": note}), config=config)
    else:
        print("    HITL 미발동 (자동 통과)")

    print(f"    -> 근거강도 {result['evidence_strength']} / 추천 {result['recommendation']} / "
          f"latency {result.get('react_latency_sec')}s / cost ${result.get('chat_input_tokens', 0)}in+"
          f"{result.get('chat_output_tokens', 0)}out tokens")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--auto-accept", action="store_true", help="HITL을 전부 ACCEPT로 자동 통과")
    parser.add_argument("--queries", type=str, help="질의 목록 파일 (한 줄당 1건)")
    args = parser.parse_args()

    if args.queries:
        with open(args.queries, encoding="utf-8") as f:
            queries = [line.strip() for line in f if line.strip()]
    else:
        queries = DEFAULT_QUERIES

    app = build_graph()
    print(f"총 {len(queries)}건 배치 실행 시작 (auto_accept={args.auto_accept})")

    ok, fail = 0, 0
    for i, q in enumerate(queries, 1):
        try:
            run_one(app, i, q, args.auto_accept)
            ok += 1
        except Exception as e:
            fail += 1
            print(f"    !! 실행 실패: {e}", file=sys.stderr)

    print(f"\n완료: 성공 {ok} / 실패 {fail}. 이제 metrics.sql로 집계하세요:")
    print("  psql -h localhost -U claimprecedent -d claimprecedent -f metrics.sql")


if __name__ == "__main__":
    main()
