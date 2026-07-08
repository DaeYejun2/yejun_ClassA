# -*- coding: utf-8 -*-
"""
ClaimPrecedent Agent — LangGraph + OpenAI function-calling 기반 ReAct 구현 (v3)

v2 대비 변경점:
1. web_search_tool 추가 — 신뢰 도메인(fss.or.kr, law.go.kr, kiri.or.kr)으로 제한된 웹검색.
   결과는 "외부참고자료"로만 취급되며 evidence_strength 산정에는 포함되지 않고,
   사용될 경우 근거강도와 무관하게 무조건 HITL이 발동함(risk_node에서 강제).
2. CRAG 우회 방지 — case_id_validator가 "실제 검색된 사례인지"뿐 아니라
   "relevance_reranker를 실제로 통과한 사례인지"까지 검증하도록 강화.
   (v2에서는 검색만 되고 재검증은 안 거친 사례를 LLM이 그냥 인용해도 걸러지지 않던 문제 수정)

가드레일(HITL/감사로그)은 v2와 동일하게 LLM 재량 밖에 둠.
"""
import os
import json
import time
from pathlib import Path
from typing import TypedDict, Optional, List, Dict, Any, Literal

import requests
from dotenv import load_dotenv
from openai import OpenAI
import psycopg2
from psycopg2.extras import RealDictCursor

from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt
from langgraph.checkpoint.memory import MemorySaver

load_dotenv(Path(__file__).parent / ".env")

EMBED_MODEL = "text-embedding-3-small"
LLM_MODEL = "gpt-4o-mini"
MAX_ITERATIONS = 8          # ReAct 루프 최대 tool-call 왕복 횟수 (웹검색 추가로 v2(6)에서 상향)
HIGH_EVIDENCE_COUNT = 3
TRUSTED_WEB_DOMAINS = ["fss.or.kr", "law.go.kr", "kiri.or.kr"]  # 웹검색 허용 도메인 (금감원/법령정보센터/보험연구원)

# 12장 token cost 지표용 단가 (2026-07 기준 gpt-4o-mini / text-embedding-3-small, USD per token)
CHAT_INPUT_RATE = 0.15 / 1_000_000
CHAT_OUTPUT_RATE = 0.60 / 1_000_000
EMBEDDING_RATE = 0.02 / 1_000_000


def get_client() -> OpenAI:
    return OpenAI(api_key=os.environ["OPENAI_API_KEY"])


def get_conn():
    return psycopg2.connect(
        host=os.environ.get("POSTGRES_HOST", "localhost"),
        port=os.environ.get("POSTGRES_PORT", "5432"),
        dbname=os.environ["POSTGRES_DB"],
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
    )


class AgentState(TypedDict, total=False):
    input_query: str
    product_type: Optional[str]
    extracted_issue: str
    retrieved_cases: List[Dict[str, Any]]
    filtered_cases: List[Dict[str, Any]]
    external_references: List[Dict[str, Any]]   # 웹검색으로 얻은 외부참고자료 (evidence_strength에 미반영)
    tool_call_trace: List[Dict[str, Any]]
    evidence_strength: Literal["LOW", "MEDIUM", "HIGH"]
    recommendation: Literal["APPROVE", "DENY", "ADDITIONAL_REVIEW_NEEDED"]
    draft_opinion: str
    hitl_required: bool
    hitl_reason: Optional[str]
    human_decision: Optional[str]
    human_note: Optional[str]
    # --- 12장 평가지표(환각율/task달성률/latency/token cost)용 신규 필드 ---
    loop_completed: bool
    hallucinated_case_ids: List[str]
    not_reranked_case_ids: List[str]
    cited_case_count: int
    first_response_latency_sec: Optional[float]
    react_latency_sec: float
    chat_input_tokens: int
    chat_output_tokens: int
    embedding_tokens: int


# ---------------------------------------------------------------------------
# Tool 스키마 (OpenAI function-calling) — report 9.4 Tool 목록 + web_search_tool 확장
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "vector_search_tool",
            "description": (
                "PostgreSQL+pgvector 기반으로 쟁점과 유사한 금융분쟁조정사례(보험 권역)를 검색한다. "
                "검색 결과가 불충분하면 검색어를 바꿔(동의어/상위개념/하위쟁점 등) 다시 호출할 수 있다."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "검색할 쟁점/키워드 문장"},
                    "top_k": {"type": "integer", "description": "반환할 최대 사례 수 (기본 5)"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "relevance_reranker",
            "description": (
                "vector_search_tool로 조회한 후보 사례 중 실제로 현재 쟁점과 관련 있는 case_id만 "
                "골라낸다 (CRAG 관련성 재검증). 이 tool을 통과하지 못한 case_id는 최종 인용에 쓸 수 없다 "
                "— case_id_validator가 이를 강제로 검증한다."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "issue": {"type": "string", "description": "판단 기준이 되는 쟁점 문장"},
                    "candidate_case_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "관련성을 재검증할 후보 case_id 목록 (이전 검색 결과 중 선택)",
                    },
                },
                "required": ["issue", "candidate_case_ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "case_id_validator",
            "description": (
                "인용하려는 case_id들이 (1) 실제로 이번 세션에서 검색됐고 (2) relevance_reranker를 "
                "통과했는지 검증한다 (환각 방지 + CRAG 우회 방지). "
                "finalize_opinion 호출 전에 인용할 모든 case_id를 이 tool로 먼저 검증할 것."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "case_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["case_ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "evidence_strength_scorer",
            "description": "현재까지 관련성 검증을 통과한 case_id 개수를 근거로 근거강도(LOW/MEDIUM/HIGH)를 산정한다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "relevant_case_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["relevant_case_ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search_tool",
            "description": (
                "내부 분쟁조정사례 코퍼스(201건)에 없는 정보를 보충할 때만 사용한다. "
                "금융감독원(fss.or.kr), 국가법령정보센터(law.go.kr), 보험연구원(kiri.or.kr) 등 "
                "신뢰 가능한 공신력 있는 도메인으로만 검색이 제한된다. "
                "이 tool의 결과는 '외부참고자료'이며 근거강도(evidence_strength) 산정에는 절대 포함되지 "
                "않고, 사용 시 반드시 사람 확인(HITL)을 거치게 된다. 내부 판례가 이미 충분하면 호출하지 말 것."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "검색할 질의 (신뢰 도메인 내에서만 검색됨)"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finalize_opinion",
            "description": (
                "판단근거 초안과 최종 참고의견을 제출하고 ReAct 루프를 종료한다. "
                "case_id_validator로 검증되지 않은 case_id는 절대 인용하지 말 것. "
                "web_search_tool 결과를 사용했다면 draft_opinion 안에서 '내부 판례 근거'와 "
                "'외부참고자료'를 명확히 구분해서 서술할 것. "
                "출력은 항상 '참고자료'이며 '최종 결정'이 아님을 draft_opinion에 명시할 것."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "issue": {"type": "string", "description": "추출한 핵심 쟁점 (한 문장)"},
                    "draft_opinion": {"type": "string"},
                    "recommendation": {
                        "type": "string",
                        "enum": ["APPROVE", "DENY", "ADDITIONAL_REVIEW_NEEDED"],
                    },
                    "cited_case_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["issue", "draft_opinion", "recommendation", "cited_case_ids"],
            },
        },
    },
]

SYSTEM_PROMPT = """너는 보험사 지급심사 담당자(심사역)를 보조하는 AI Agent다.
심사역이 입력한, 보장범위 해석이 애매한 청구 건에 대해 유사 금융분쟁조정사례를 근거로
참고의견을 제시하는 것이 목적이다. 최종 지급 결정 권한은 없으며, 너의 출력은 항상 "참고자료"다.

사용 가능한 tool: vector_search_tool, relevance_reranker, case_id_validator,
evidence_strength_scorer, web_search_tool, finalize_opinion.

반드시 지킬 것:
1. 실제로 vector_search_tool로 검색된 case_id만 사용할 것. 존재하지 않는 사례를 지어내지 말 것.
2. relevance_reranker를 통과하지 못한(또는 아예 넣지 않은) case_id는 인용하지 말 것 — 검색만 되고
   관련성 재검증을 거치지 않은 사례를 근거로 쓰는 것은 금지된다.
3. 첫 검색 결과가 빈약하거나 관련성이 낮으면, 검색어를 바꿔 vector_search_tool을 다시 호출해도 된다.
4. finalize_opinion을 호출하기 전에 반드시 case_id_validator로 인용할 case_id를 검증할 것.
5. web_search_tool은 내부 판례만으로 부족할 때만 보조적으로 사용하고, 그 결과는 '외부참고자료'로만
   취급해 draft_opinion에서 내부 판례 근거와 명확히 구분해서 서술할 것.
6. 관련 사례를 전혀 찾지 못했다면, 억지로 결론 내지 말고 그 사실을 draft_opinion에
   명시하고 recommendation은 ADDITIONAL_REVIEW_NEEDED로 finalize_opinion을 호출할 것.
7. 판단이 끝나면 반드시 finalize_opinion을 호출해야 루프가 종료된다."""


# ---------------------------------------------------------------------------
# Tool 실행부 (실제 DB/재검증/외부검색 로직)
# ---------------------------------------------------------------------------

class ToolContext:
    def __init__(self):
        self.seen_cases: Dict[str, Dict[str, Any]] = {}       # 실제 DB에서 검색된 사례만 등록
        self.reranked_relevant: set = set()                    # relevance_reranker를 통과한 case_id만 등록
        self.web_sources: List[Dict[str, Any]] = []            # web_search_tool로 얻은 외부참고자료
        self.trace: List[Dict[str, Any]] = []
        # --- 12장 token cost 지표용 누적 카운터 ---
        self.chat_input_tokens: int = 0
        self.chat_output_tokens: int = 0
        self.embedding_tokens: int = 0


def exec_vector_search_tool(ctx: ToolContext, query: str, top_k: int = 5) -> Dict[str, Any]:
    client = get_client()
    emb_resp = client.embeddings.create(model=EMBED_MODEL, input=[query])
    ctx.embedding_tokens += emb_resp.usage.total_tokens  # 12장 token cost 지표용
    emb = emb_resp.data[0].embedding

    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        """
        SELECT case_id, domain, case_type, title, issue, resolution,
               1 - (embedding <=> %s::vector) AS similarity
        FROM fss_dispute_cases
        WHERE domain = '보험'
        ORDER BY embedding <=> %s::vector
        LIMIT %s
        """,
        (emb, emb, top_k or 5),
    )
    rows = [dict(r) for r in cur.fetchall()]
    cur.close()
    conn.close()

    for r in rows:
        ctx.seen_cases[r["case_id"]] = r

    return {
        "results": [
            {"case_id": r["case_id"], "title": r["title"], "issue": r["issue"],
             "similarity": round(r["similarity"], 3)}
            for r in rows
        ]
    }


def exec_relevance_reranker(ctx: ToolContext, issue: str, candidate_case_ids: List[str]) -> Dict[str, Any]:
    candidates = [ctx.seen_cases[cid] for cid in candidate_case_ids if cid in ctx.seen_cases]
    if not candidates:
        return {"relevant_case_ids": []}

    client = get_client()
    case_list_text = "\n".join(f"- {c['case_id']}: {c['title']} / 쟁점: {c['issue']}" for c in candidates)
    prompt = f"""아래는 벡터검색으로 조회된 후보 분쟁조정사례 목록이다.
쟁점과 실제로 관련이 있는 case_id만 골라 JSON으로 출력하라. 관련성이 낮거나 애매하면 제외할 것.

쟁점: {issue}

후보 사례:
{case_list_text}

출력 형식: {{"relevant_case_ids": ["FSS-0001", ...]}} (없으면 빈 배열)"""
    resp = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
        response_format={"type": "json_object"},
    )
    ctx.chat_input_tokens += resp.usage.prompt_tokens        # 12장 token cost 지표용
    ctx.chat_output_tokens += resp.usage.completion_tokens   # 12장 token cost 지표용
    try:
        relevant = json.loads(resp.choices[0].message.content).get("relevant_case_ids", [])
    except (json.JSONDecodeError, AttributeError):
        relevant = []
    # 버그 수정: 세션 전체(ctx.seen_cases)가 아니라 "이번 호출에 실제로 넘겨진 candidate_case_ids"로만
    # 제한해야 함. 세션 전체 기준으로 필터링하면, 이전에 다른 검색으로 seen_cases에 들어간 case_id를
    # nested LLM이 이번 candidate 목록에 없었는데도 relevant로 잘못 반환했을 때 걸러내지 못함
    # (실제 데모에서 candidate_case_ids=[FSS-0014,FSS-0056,FSS-0026]인데 FSS-0201이
    #  reranked_relevant에 섞여 들어간 사례로 확인됨).
    relevant = [cid for cid in relevant if cid in candidate_case_ids and cid in ctx.seen_cases]
    ctx.reranked_relevant.update(relevant)
    return {"relevant_case_ids": relevant}


def exec_case_id_validator(ctx: ToolContext, case_ids: List[str]) -> Dict[str, Any]:
    valid, invalid_hallucinated, invalid_not_reranked = [], [], []
    for cid in case_ids:
        if cid not in ctx.seen_cases:
            invalid_hallucinated.append(cid)          # 이번 세션에 검색된 적 자체가 없음 (환각)
        elif cid not in ctx.reranked_relevant:
            invalid_not_reranked.append(cid)           # 검색은 됐지만 relevance_reranker 미통과 (CRAG 우회)
        else:
            valid.append(cid)
    return {
        "valid_case_ids": valid,
        "invalid_hallucinated_case_ids": invalid_hallucinated,
        "invalid_not_reranked_case_ids": invalid_not_reranked,
        "note": "invalid로 분류된 case_id는 인용 금지 — hallucinated는 미검색 사례, not_reranked는 관련성 재검증 미통과 사례",
    }


def exec_evidence_strength_scorer(relevant_case_ids: List[str]) -> Dict[str, Any]:
    n = len(relevant_case_ids)
    strength = "LOW" if n == 0 else ("HIGH" if n >= HIGH_EVIDENCE_COUNT else "MEDIUM")
    return {"evidence_strength": strength, "relevant_case_count": n}


def exec_web_search_tool(ctx: ToolContext, query: str) -> Dict[str, Any]:
    api_key = os.environ.get("SERPER_API_KEY")
    if not api_key:
        return {"error": "SERPER_API_KEY가 설정되지 않아 웹검색을 사용할 수 없습니다."}

    site_filter = " OR ".join(f"site:{d}" for d in TRUSTED_WEB_DOMAINS)
    full_query = f"{query} ({site_filter})"

    try:
        resp = requests.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            json={"q": full_query, "num": 5},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        return {"error": f"웹검색 요청 실패: {e}"}

    results = []
    for item in (data.get("organic") or [])[:3]:
        entry = {"title": item.get("title"), "url": item.get("link"), "snippet": item.get("snippet")}
        results.append(entry)
        ctx.web_sources.append(entry)

    return {
        "results": results,
        "note": "이 결과는 '외부참고자료'이며 근거강도 산정에는 포함되지 않습니다. draft_opinion에서 내부 판례와 구분해서 서술할 것.",
    }


TOOL_EXECUTORS = {
    "vector_search_tool": lambda ctx, args: exec_vector_search_tool(ctx, **args),
    "relevance_reranker": lambda ctx, args: exec_relevance_reranker(ctx, **args),
    "case_id_validator": lambda ctx, args: exec_case_id_validator(ctx, **args),
    "evidence_strength_scorer": lambda ctx, args: exec_evidence_strength_scorer(**args),
    "web_search_tool": lambda ctx, args: exec_web_search_tool(ctx, **args),
}


# ---------------------------------------------------------------------------
# ReAct 루프 노드
# ---------------------------------------------------------------------------

def react_node(state: AgentState) -> AgentState:
    client = get_client()
    ctx = ToolContext()
    t_start = time.monotonic()               # 12장 avg latency 지표용
    first_response_latency: Optional[float] = None   # 12장 avg latency 지표용 (첫 LLM 응답까지)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": (
            f"질의: {state['input_query']}\n상품유형: {state.get('product_type') or '미상'}"
        )},
    ]

    final_args: Optional[Dict[str, Any]] = None

    for step in range(MAX_ITERATIONS):
        resp = client.chat.completions.create(
            model=LLM_MODEL, messages=messages, tools=TOOLS, tool_choice="auto", temperature=0,
        )
        ctx.chat_input_tokens += resp.usage.prompt_tokens        # 12장 token cost 지표용
        ctx.chat_output_tokens += resp.usage.completion_tokens   # 12장 token cost 지표용
        if first_response_latency is None:                       # 12장 avg latency 지표용
            first_response_latency = time.monotonic() - t_start
        msg = resp.choices[0].message

        if not msg.tool_calls:
            messages.append({"role": "assistant", "content": msg.content or ""})
            messages.append({"role": "user", "content": "finalize_opinion tool을 호출해서 결론을 제출하라."})
            continue

        messages.append({
            "role": "assistant",
            "content": msg.content,
            "tool_calls": [tc.model_dump() for tc in msg.tool_calls],
        })

        stop = False
        for tc in msg.tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}

            if name == "finalize_opinion":
                final_args = args
                result = {"status": "finalized"}
                stop = True
            elif name in TOOL_EXECUTORS:
                try:
                    result = TOOL_EXECUTORS[name](ctx, args)
                except Exception as e:
                    result = {"error": str(e)}
            else:
                result = {"error": f"unknown tool: {name}"}

            ctx.trace.append({"step": step, "tool": name, "arguments": args, "result": result})
            messages.append({
                "role": "tool", "tool_call_id": tc.id, "content": json.dumps(result, ensure_ascii=False),
            })

        if stop:
            break

    react_latency = time.monotonic() - t_start   # 12장 avg latency 지표용

    if final_args is None:
        return {
            "extracted_issue": state["input_query"],
            "retrieved_cases": list(ctx.seen_cases.values()),
            "filtered_cases": [],
            "external_references": ctx.web_sources,
            "tool_call_trace": ctx.trace,
            "evidence_strength": "LOW",
            "draft_opinion": "판단 루프가 최대 반복 횟수 내에 결론을 내지 못했습니다. 사람의 직접 판단이 필요합니다. (참고자료입니다)",
            "recommendation": "ADDITIONAL_REVIEW_NEEDED",
            # --- 12장 평가지표용 ---
            "loop_completed": False,
            "hallucinated_case_ids": [],
            "not_reranked_case_ids": [],
            "cited_case_count": 0,
            "first_response_latency_sec": round(first_response_latency, 3) if first_response_latency is not None else None,
            "react_latency_sec": round(react_latency, 3),
            "chat_input_tokens": ctx.chat_input_tokens,
            "chat_output_tokens": ctx.chat_output_tokens,
            "embedding_tokens": ctx.embedding_tokens,
        }

    # --- 서버 측 독립 재검증 (LLM 자기신고를 그대로 믿지 않음) ---
    cited = final_args.get("cited_case_ids", []) or []
    validated = exec_case_id_validator(ctx, cited)
    valid_cited = validated["valid_case_ids"]
    hallucinated = validated["invalid_hallucinated_case_ids"]
    not_reranked = validated["invalid_not_reranked_case_ids"]

    filtered_cases = [ctx.seen_cases[cid] for cid in valid_cited]
    scored = exec_evidence_strength_scorer(valid_cited)

    recommendation = final_args.get("recommendation", "ADDITIONAL_REVIEW_NEEDED")
    if recommendation not in ("APPROVE", "DENY", "ADDITIONAL_REVIEW_NEEDED"):
        recommendation = "ADDITIONAL_REVIEW_NEEDED"

    draft = final_args.get("draft_opinion", "")
    if hallucinated:
        draft += f"\n[검증 경고] 이번 세션에서 실제로 검색되지 않은 case_id({', '.join(hallucinated)})가 감지되어 제외했습니다."
        recommendation = "ADDITIONAL_REVIEW_NEEDED"
    if not_reranked:
        draft += f"\n[검증 경고] 검색은 됐으나 관련성 재검증(CRAG)을 통과하지 못한 case_id({', '.join(not_reranked)})가 감지되어 제외했습니다."
        recommendation = "ADDITIONAL_REVIEW_NEEDED"

    return {
        "extracted_issue": final_args.get("issue", state["input_query"]),
        "retrieved_cases": list(ctx.seen_cases.values()),
        "filtered_cases": filtered_cases,
        "external_references": ctx.web_sources,
        "tool_call_trace": ctx.trace,
        "evidence_strength": scored["evidence_strength"],
        "draft_opinion": draft,
        "recommendation": recommendation,
        # --- 12장 평가지표용 ---
        "loop_completed": True,
        "hallucinated_case_ids": hallucinated,
        "not_reranked_case_ids": not_reranked,
        "cited_case_count": len(cited),
        "first_response_latency_sec": round(first_response_latency, 3) if first_response_latency is not None else None,
        "react_latency_sec": round(react_latency, 3),
        "chat_input_tokens": ctx.chat_input_tokens,
        "chat_output_tokens": ctx.chat_output_tokens,
        "embedding_tokens": ctx.embedding_tokens,
    }


# ---------------------------------------------------------------------------
# 결정론적 가드레일 노드 (LLM 재량 밖)
# ---------------------------------------------------------------------------

def risk_node(state: AgentState) -> AgentState:
    reasons = []
    if not state["filtered_cases"] or state["evidence_strength"] == "LOW":
        reasons.append("유사 판례가 없거나 관련성이 낮음")
    if state["recommendation"] == "DENY":
        reasons.append("참고의견이 반려(소비자 불리) 방향")
    if state["recommendation"] == "ADDITIONAL_REVIEW_NEEDED":
        reasons.append("AI 추천 자체가 추가검토 필요(ADDITIONAL_REVIEW_NEEDED)로, 근거강도와 무관하게 사람 확인 필요")
    if state["evidence_strength"] in ("LOW", "MEDIUM"):
        reasons.append("근거강도 점수가 임계치 이하")
    if state.get("external_references"):
        reasons.append("외부 웹 검색 결과(참고자료)가 사용되어 사람 확인 필요")

    hitl_required = len(reasons) > 0
    return {"hitl_required": hitl_required, "hitl_reason": "; ".join(reasons) if reasons else None}


def hitl_node(state: AgentState) -> AgentState:
    if not state.get("hitl_required"):
        return {"human_decision": "AUTO_PASS", "human_note": None}

    payload = {
        "extracted_issue": state["extracted_issue"],
        "draft_opinion": state["draft_opinion"],
        "recommendation": state["recommendation"],
        "evidence_strength": state["evidence_strength"],
        "hitl_reason": state["hitl_reason"],
        "external_references": state.get("external_references", []),
    }
    decision = interrupt(payload)
    return {"human_decision": decision.get("decision"), "human_note": decision.get("note")}


def log_node(state: AgentState) -> AgentState:
    conn = get_conn()
    cur = conn.cursor()

    # 12장 token cost 지표: 케이스당 예상 비용(USD) 계산
    estimated_cost = (
        state.get("chat_input_tokens", 0) * CHAT_INPUT_RATE
        + state.get("chat_output_tokens", 0) * CHAT_OUTPUT_RATE
        + state.get("embedding_tokens", 0) * EMBEDDING_RATE
    )

    cur.execute(
        """
        INSERT INTO agent_audit_log
            (input_query, extracted_issue, retrieved_case_ids, filtered_case_ids,
             evidence_strength, recommendation, hitl_required, hitl_reason,
             human_decision, human_note, external_references,
             loop_completed, hallucinated_case_ids, not_reranked_case_ids, cited_case_count,
             first_response_latency_sec, react_latency_sec,
             chat_input_tokens, chat_output_tokens, embedding_tokens, estimated_cost_usd)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            state["input_query"],
            state["extracted_issue"],
            [c["case_id"] for c in state.get("retrieved_cases", [])],
            [c["case_id"] for c in state.get("filtered_cases", [])],
            state["evidence_strength"],
            state["recommendation"],
            state["hitl_required"],
            state.get("hitl_reason"),
            state.get("human_decision"),
            state.get("human_note"),
            json.dumps(state.get("external_references", []), ensure_ascii=False),
            state.get("loop_completed"),
            state.get("hallucinated_case_ids", []),
            state.get("not_reranked_case_ids", []),
            state.get("cited_case_count", 0),
            state.get("first_response_latency_sec"),
            state.get("react_latency_sec"),
            state.get("chat_input_tokens", 0),
            state.get("chat_output_tokens", 0),
            state.get("embedding_tokens", 0),
            round(estimated_cost, 6),
        ),
    )
    conn.commit()
    cur.close()
    conn.close()
    return {}


# ---------------------------------------------------------------------------
# 그래프 조립
# ---------------------------------------------------------------------------

def build_graph():
    graph = StateGraph(AgentState)
    graph.add_node("react", react_node)
    graph.add_node("risk", risk_node)
    graph.add_node("hitl", hitl_node)
    graph.add_node("log", log_node)

    graph.add_edge(START, "react")
    graph.add_edge("react", "risk")
    graph.add_edge("risk", "hitl")
    graph.add_edge("hitl", "log")
    graph.add_edge("log", END)

    checkpointer = MemorySaver()
    return graph.compile(checkpointer=checkpointer)


if __name__ == "__main__":
    app = build_graph()
    print("그래프 컴파일 성공. 노드:", list(app.get_graph().nodes.keys()))
