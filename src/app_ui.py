# -*- coding: utf-8 -*-
"""
ClaimPrecedent 심사역 UI — Streamlit 데모
아키텍처 1계층(심사역 UI)의 실물 구현: 질의 입력 → tool 이력 → 판례 카드 → HITL 승인 게이트

실행 (8502 사용):
    streamlit run app_ui.py --server.port 8502 --server.address 127.0.0.1 --server.headless true
접속 (기본 — SSH 터널, 본인만):
    ssh -L 8502:localhost:8502 opc@<VM_IP>  →  브라우저에서 http://localhost:8502
임시 공개 (발표용, nginx 서브경로 /agent 프록시 사용 시):
    streamlit run app_ui.py --server.port 8502 --server.address 127.0.0.1 \
        --server.headless true --server.baseUrlPath /agent
공개 시 보호: .env에 UI_PASSWORD=원하는비번 을 설정하면 접속 시 비밀번호를 요구함
    (미설정 시 잠금 없음 — SSH 터널 사용 시 그대로 두면 됨)
"""
import os
import time
import uuid

import streamlit as st
from langgraph.types import Command

from agent import build_graph

# ── 페이지 설정 ──────────────────────────────────────────────────────────────
st.set_page_config(page_title="ClaimPrecedent — 지급판단 보조 Agent", layout="wide")

NAVY = "#1E2761"
st.markdown(f"""
<style>
  .stApp h1 {{ color: {NAVY}; }}
  .badge {{ display:inline-block; padding:4px 14px; border-radius:14px;
            font-weight:700; font-size:0.95rem; color:white; }}
  .b-high {{ background:#2E7D5B; }} .b-med {{ background:#C46A1B; }} .b-low {{ background:#B3402F; }}

  .notice {{ background:#EEF4FE; border-left:4px solid #1E2761; padding:10px 14px;
             border-radius:4px; font-size:0.9rem; color:#1F2430; }}


</style>
""", unsafe_allow_html=True)

EXAMPLES = {
    "정상 인용 (시나리오 A)": "실손보험 가입자가 A 시술을 받고 진료비 청구. 치료 목적과 미용 목적이 혼재되어 보이는데, 유사 분쟁조정사례가 있는지 확인해줘.",
    "오류 방어 (시나리오 B)": "음주운전 면책 조항 적용 여부가 쟁점인 상해보험 청구 건.",
    "조기 기권 (시나리오 C)": "실손보험 가입자가 도수치료를 장기간 반복해서 받았는데 보험사가 치료 목적이 아니라며 지급을 거절함. 유사 사례 확인 요청.",
}

STRENGTH_BADGE = {"HIGH": "b-high", "MEDIUM": "b-med", "LOW": "b-low"}
REC_LABEL = {"APPROVE": "✅ 승인 방향", "DENY": "⛔ 반려 방향(소비자 불리)", "ADDITIONAL_REVIEW_NEEDED": "🔍 추가검토 필요"}


@st.cache_resource
def get_app():
    return build_graph()


def init_state():
# Streamlit 세션 상태 초기화. phase: idle(입력 대기) → awaiting_hitl(승인 대기) → done.
    ss = st.session_state
    ss.setdefault("phase", "idle")          # idle | awaiting_hitl | done
    ss.setdefault("result", None)
    ss.setdefault("interrupt_payload", None)
    ss.setdefault("thread_id", None)
    ss.setdefault("query_text", "")


def run_agent(query: str, product_type: str):
# 그래프 실행. HITL 발동 시 interrupt payload를 보관하고 phase를 awaiting_hitl로 전환.
    ss = st.session_state
    ss.thread_id = f"ui-{uuid.uuid4().hex[:8]}"
    config = {"configurable": {"thread_id": ss.thread_id}}
    t0 = time.time()
    with st.spinner("Agent가 유사 판례를 검색·재검증하는 중입니다..."):
        result = get_app().invoke({"input_query": query, "product_type": product_type}, config=config)
    ss.result = result
    ss.elapsed = time.time() - t0
    if "__interrupt__" in result:
        ss.interrupt_payload = result["__interrupt__"][0].value
        ss.phase = "awaiting_hitl"
    else:
        ss.interrupt_payload = None
        ss.phase = "done"


def resume_agent(decision: str, note: str):
    """심사역 판정(ACCEPT/MODIFY/REJECT)으로 중단된 그래프를 재개 — 같은 thread_id의
    checkpointer 상태에서 이어 실행되고 감사로그가 저장된다."""
    ss = st.session_state
    config = {"configurable": {"thread_id": ss.thread_id}}
    with st.spinner("심사역 결정을 반영하고 감사로그를 저장하는 중..."):
        ss.result = get_app().invoke(Command(resume={"decision": decision, "note": note}), config=config)
    ss.phase = "done"


def render_trace(result):
    """tool 호출 이력 표시 (조기 기권 가드레일 발동은 🛑 아이콘으로 구분)."""
    trace = result.get("tool_call_trace", [])
    with st.expander(f"🔧 Agent tool 호출 이력 — 자율 ReAct 루프 ({len(trace)}회)", expanded=False):
        for i, t in enumerate(trace, 1):
            tool = t.get("tool", "")
            icon = "🛑" if tool == "early_abstain_guardrail" else ("🏁" if tool == "finalize_opinion" else "▶️")
            st.markdown(f"**{icon} [{i}] `{tool}`**")
            st.code(f"args   = {t.get('arguments')}\nresult = {t.get('result')}", language="text")


def render_cases(result):
    """인용 판례 카드: 쟁점·처리결과 원문을 펼쳐볼 수 있어 심사역 판정의 근거가 된다."""
    cases = result.get("filtered_cases", [])
    if not cases:
        st.info("인용된 판례 없음 — 코퍼스 커버리지 밖 쟁점 (기권)")
        return
    st.markdown(f"**📚 인용 판례 {len(cases)}건** (관련성 재검증·이중검증 통과)")
    for c in cases:
        sim = c.get("similarity")
        head = f"{c.get('case_id')} · {c.get('title')}" + (f"  (유사도 {sim:.3f})" if isinstance(sim, float) else "")
        with st.expander(head):
            st.markdown(f"- **유형**: {c.get('case_type', '-')}")
            st.markdown(f"- **쟁점**: {c.get('issue', '-')}")
            st.markdown(f"- **처리결과**: {c.get('resolution', '-')}")


def render_opinion(result):
    """근거강도 배지 + AI 추천 + 참고의견 + 외부자료(구분 표시) 렌더링."""
    strength = result.get("evidence_strength", "LOW")
    rec = result.get("recommendation", "ADDITIONAL_REVIEW_NEEDED")
    c1, c2, c3 = st.columns([1.2, 2, 1.5])
    with c1:
        st.markdown(f'근거강도&nbsp; <span class="badge {STRENGTH_BADGE.get(strength, "b-low")}">{strength}</span>', unsafe_allow_html=True)
    with c2:
        st.markdown(f"**AI 추천**: {REC_LABEL.get(rec, rec)}")
    with c3:
        if result.get("react_latency_sec") is not None:
            st.markdown(f"⏱ 처리 {result['react_latency_sec']}초")
    st.markdown(f'<div class="notice"><b>AI 참고의견</b><br>{result.get("draft_opinion", "")}</div>', unsafe_allow_html=True)
    if result.get("external_references"):
        st.markdown("**🌐 외부참고자료** (웹검색 · 근거강도 미반영)")
        for ref in result["external_references"]:
            st.markdown(f"- [{ref.get('title')}]({ref.get('url')})")


# ── 메인 ────────────────────────────────────────────────────────────────────
init_state()
ss = st.session_state

# 접속 게이트 (공개 배포 시 API 비용·감사로그 오염 방지) — .env의 UI_PASSWORD 설정 시에만 활성화
_pw = os.environ.get("UI_PASSWORD", "")
if _pw:
    ss.setdefault("authed", False)
    if not ss.authed:
        st.title("ClaimPrecedent")
        entered = st.text_input("접속 비밀번호", type="password")
        if st.button("입장"):
            if entered == _pw:
                ss.authed = True
                st.rerun()
            else:
                st.error("비밀번호가 올바르지 않습니다.")
        st.stop()

st.title("ClaimPrecedent")
st.caption("보험 지급심사 담당자를 위한 유사 분쟁사례 기반 지급판단 보조 AI Agent — 산출물은 참고자료이며 최종 결정은 심사역에게 있습니다")

with st.sidebar:
    st.header("케이스 입력")
    product_type = st.selectbox("상품유형", ["실손의료보험", "질병·상해보험", "생명보험", "자동차보험", "기타"])
    st.markdown("**예시 질의**")
    for label, q in EXAMPLES.items():
        if st.button(label, use_container_width=True):
            ss.query_text = q
            ss.phase = "idle"
    st.divider()
    st.caption("구조: LangGraph — react(자율) → risk → hitl → log(결정론적)\n\n모든 실행은 agent_audit_log에 감사로그로 저장됩니다.")

query = st.text_area("청구 건 요약 / 질의", value=ss.query_text, height=90,
                     placeholder="예: 백내장 수술 후 다초점 인공수정체 비용을 실손으로 청구했는데 보험사가 시력교정 목적이라며 거절. 판례 확인.")

if st.button("🔎 유사 판례 검색 및 참고의견 생성", type="primary", disabled=(ss.phase == "awaiting_hitl")):
    if query.strip():
        ss.query_text = query
        run_agent(query.strip(), product_type)
        st.rerun()
    else:
        st.warning("질의를 입력해 주세요.")

# ── 결과 표시 ────────────────────────────────────────────────────────────────
if ss.phase in ("awaiting_hitl", "done") and ss.result:
    result = ss.interrupt_payload if ss.phase == "awaiting_hitl" else ss.result
    st.divider()
    st.subheader("Agent 분석 결과")
    if ss.result.get("extracted_issue"):
        st.markdown(f"**추출된 쟁점**: {ss.result['extracted_issue']}")
    render_trace(ss.result)
    render_cases(ss.result)
    render_opinion(result if ss.phase == "awaiting_hitl" else ss.result)

if ss.phase == "awaiting_hitl":
    p = ss.interrupt_payload
    st.divider()
    st.subheader("🙋 Human-in-the-loop 승인 게이트")
    st.error(f"**발동 사유**: {p.get('hitl_reason')}")
    st.markdown("⚠️ 위 내용은 **참고자료**이며 최종 결정이 아닙니다. 심사역의 판정이 필요합니다.")
    note = st.text_input("코멘트 (선택)", key="hitl_note")
    b1, b2, b3 = st.columns(3)
    if b1.button("✅ ACCEPT — 초안 수용", use_container_width=True):
        resume_agent("ACCEPT", note); st.rerun()
    if b2.button("✏️ MODIFY — 수정 필요", use_container_width=True):
        resume_agent("MODIFY", note); st.rerun()
    if b3.button("⛔ REJECT — 초안 기각", use_container_width=True):
        resume_agent("REJECT", note); st.rerun()

if ss.phase == "done" and ss.result:
    st.divider()
    if ss.result.get("hitl_required"):
        d = ss.result.get("human_decision")
        icon = {"ACCEPT": "✅", "MODIFY": "✏️", "REJECT": "⛔"}.get(d, "☑️")
        st.success(f"{icon} 심사역 최종 판정: **{d}**" + (f" · 코멘트: {ss.result.get('human_note')}" if ss.result.get("human_note") else ""))
    else:
        st.success("☑️ 자동 통과 (HIGH + APPROVE + 웹검색 미사용) — HITL 미발동")
    st.caption("본 실행의 입력·출력·판정 결과는 agent_audit_log 테이블에 감사로그로 저장되었습니다.")
    if st.button("새 케이스 입력"):
        ss.phase, ss.result, ss.interrupt_payload, ss.query_text = "idle", None, None, ""
        st.rerun()
