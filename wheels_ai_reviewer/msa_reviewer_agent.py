"""
MSA Reviewer Agent — LangGraph prototype (Azure AI Foundry edition)
====================================================================
Fans out one "specialist reviewer" LLM call per checklist category
(run in parallel via LangGraph's Send API), then aggregates the
findings into a single scored review report.

Model + document source have been swapped for an Azure stack:
    - LLM calls go to Claude models deployed on Azure AI Foundry
      (native Anthropic Messages API at <resource>/anthropic)
    - Input document is markdown produced by Azure Document Intelligence,
      not a raw .docx

Run:
    export ANTHROPIC_FOUNDRY_RESOURCE=<your-foundry-resource-name>
    export ANTHROPIC_FOUNDRY_API_KEY=...      # or use Entra ID, see get_llm()
    python msa_reviewer_agent.py path/to/contract.md

Extend later:
    - tier models per category (Opus for service_calculations/liability_data,
      Haiku for schedules_exhibits) once you've validated Sonnet's output — see
      MODEL_ROUTING below
    - add a Send-fanned "per-vehicle-schedule" reviewer for numeric checks
    - persist findings to a DB / render to docx via the docx skill
"""

import json
import math
import os
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from itertools import cycle
from typing import Annotated, TypedDict

from langchain_anthropic import ChatAnthropic
from langgraph.graph import StateGraph, START, END
from langgraph.constants import Send
from pydantic import BaseModel, Field, field_validator
import operator
import config

# ---------------------------------------------------------------------------
# 0. Azure AI Foundry client setup
# ---------------------------------------------------------------------------
#
# Foundry's Claude deployments speak the native Anthropic Messages API at
#   https://<resource-name>.services.ai.azure.com/anthropic
# so ChatAnthropic just needs base_url + auth swapped — nothing else in the
# graph changes. `model=` below must match your Foundry *deployment name*,
# not necessarily the raw model ID (pin an exact version when you deploy it).

FOUNDRY_RESOURCE = os.environ.get("ANTHROPIC_FOUNDRY_RESOURCE") # only used for preparing the url.
# FOUNDRY_BASE_URL = (
#     config.DEPlOYMENT_ENDPOINT
#     or (f"https://{FOUNDRY_RESOURCE}.services.ai.azure.com/anthropic" if FOUNDRY_RESOURCE else None)
# )
FOUNDRY_BASE_URL = config.DEPLOYMENT_ENDPOINT or  ""

# Per-category model routing. Start with everything on Sonnet; upgrade the
# highest-stakes categories to Opus once the prototype is validated.
DEFAULT_MODEL = config.DEPLOYMENT_NAME         # your Foundry Sonnet deployment name
MODEL_ROUTING: dict[str, str] = {
    # "service_calculations": "claude-opus-4-8",
    # "liability_data": "claude-opus-4-8",
    # "schedules_exhibits": "claude-haiku-4-5",
}


def get_llm(model: str = DEFAULT_MODEL) -> ChatAnthropic:
    """Build a ChatAnthropic client pointed at Azure AI Foundry.

    Two auth options:
      A) API key (fastest for a prototype):
           export ANTHROPIC_FOUNDRY_API_KEY=...
      B) Microsoft Entra ID (recommended once this leaves prototype stage —
           avoids a long-lived key). Uncomment the token-provider block below
           and swap `anthropic_api_key=` for `default_headers=`.
    """
    if FOUNDRY_BASE_URL:
        api_key = config.DEPLOYMENT_KEY or ""
        return ChatAnthropic(
            model=model,
            base_url=FOUNDRY_BASE_URL,
            anthropic_api_key=api_key,
            default_headers={"anthropic-version": "2023-06-01"},
        )
        # --- Option B: Entra ID instead of a static key ---
        # from azure.identity import DefaultAzureCredential, get_bearer_token_provider
        # token_provider = get_bearer_token_provider(
        #     DefaultAzureCredential(), "https://ai.azure.com/.default"
        # )
        # return ChatAnthropic(
        #     model=model,
        #     base_url=FOUNDRY_BASE_URL,
        #     anthropic_api_key="placeholder",  # required by the client, unused
        #     default_headers={
        #         "Authorization": f"Bearer {token_provider()}",
        #         "anthropic-version": "2023-06-01",
        #     },
        # )
    # Fallback: direct Anthropic API (useful for local dev without Azure creds)
    return ChatAnthropic(model=model, temperature=0)

# ---------------------------------------------------------------------------
# 1. Checklist definition — grounded in the fleet-management review checklist
#    Each category gets its own reviewer persona/system prompt.
# ---------------------------------------------------------------------------

CHECKLIST: dict[str, dict] = {
    "definitions_scope": {
        "persona": "a commercial contracts lawyer specializing in fleet leasing",
        "items": [
            "Legal entity names match incorporation documents, not trade names",
            "Wheels is consistently the Lessor/Service Provider throughout — no role reversal",
            "Defined terms are used consistently and none are orphaned/undefined",
            "Ownership/title language matches the companion Master Lease Agreement",
        ],
    },
    "onboarding_operations": {
        "persona": "a fleet management operations manager who runs client onboarding",
        "items": [
            "Onboarding SLA (business days to system setup) is achievable for a manual PDF/email intake process",
            "No gap where services (e.g. fuel card) could be active before billing setup is complete",
            "Document submission process matches how Finance & Billing actually operates today",
        ],
    },
    "service_calculations": {
        "persona": (
            "a fleet management billing analyst with deep expertise in fuel, mileage, "
            "toll, telematics, and insurance program calculations"
        ),
        "items": [
            "Fuel pass-through is tied to actual pump price with a named reconciliation data source",
            "Mileage allowance is realistic for the fleet's vehicle class/use case, not a generic default",
            "Mileage true-up mechanic (monthly vs cumulative) is unambiguous and matches billing system capability",
            "Toll liability split for failed transponder reads is fair and enforceable",
            "Telematics pro-ration method matches how the billing system actually pro-rates",
            "Insurance Rate Classes referenced are actually defined in a schedule that exists",
            "No double-billing risk between overlapping services (e.g. insurance premium vs waiver)",
        ],
    },
    "billing_payment": {
        "persona": "a fleet management billing analyst",
        "items": [
            "Payment term and late interest are within usury limits of the governing state",
            "Dispute window is realistic for a client auditing a multi-vehicle, multi-service invoice",
            "Suspension-for-nonpayment clause doesn't inadvertently cut compliance-critical services",
            "Reconciliation cap aligns with how long fuel/toll data actually takes to settle",
        ],
    },
    "fees_adjustments": {
        "persona": "a fleet management pricing/finance analyst",
        "items": [
            "CPI-linked escalation explicitly excludes market-driven pass-through costs (fuel, toll, insurance)",
            "Extraordinary adjustment / service-termination right is operationally workable against the MLA",
        ],
    },
    "liability_data": {
        "persona": "a commercial contracts lawyer specializing in liability and data privacy",
        "items": [
            "Liability cap scope (what's excluded, e.g. Rentals) is intentional and doesn't create a coverage gap",
            "Confidentiality/data clauses satisfy telematics/GPS privacy obligations in the operating jurisdiction",
            "Insurance claims carve-out from the liability cap is legally sound in the governing state",
        ],
    },
    "termination_boilerplate": {
        "persona": "a commercial contracts lawyer",
        "items": [
            "Termination of the MSA vs MLA interaction is intentional, not an oversight",
            "Assignment clause balances Wheels' M&A flexibility against client counterparty risk",
            "Arbitration seat/institution placeholders are flagged as required-before-execution",
        ],
    },
    "schedules_exhibits": {
        "persona": "a fleet management billing analyst",
        "items": [
            "Every rate in the fee schedule traces to a formula in the body of the agreement",
            "Worked examples are numerically consistent with the fee schedule",
        ],
    },
}


# ---------------------------------------------------------------------------
# 2. Structured output schema — forces the model to return checkable findings
# ---------------------------------------------------------------------------

class Finding(BaseModel):
    category: str
    checklist_item: str
    status: str = Field(description="pass | flag | fail")
    severity: str = Field(description="low | medium | high")
    clause_reference: str = Field(description="Article/Schedule number or 'not found'")
    issue: str = Field(description="What's wrong, or why it passes")
    recommendation: str = Field(description="Concrete fix, or 'none' if passing")


class CategoryReview(BaseModel):
    findings: list[Finding]

    @field_validator("findings", mode="before")
    @classmethod
    def _coerce_stringified_findings(cls, v):
        """Some gateways (observed via the Azure AI Foundry Anthropic
        passthrough) double-encode structured-output tool arguments, so
        'findings' comes back as a JSON string like '[{"category": ...}]'
        instead of an already-parsed list. Pydantic's list_type validation
        rejects that outright (the 'Input should be a valid list
        [type=list_type]' error). If we got a string, parse it as JSON before
        validation proceeds; if that fails, raise a clearer error.
        """
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except json.JSONDecodeError as e:
                raise ValueError(
                    f"'findings' was returned as a string and isn't valid JSON: {e}"
                ) from e
        return v


# ---------------------------------------------------------------------------
# 3. Graph state
# ---------------------------------------------------------------------------

class ReviewState(TypedDict):
    doc_text: str
    sections: dict          # {"ARTICLE 5": "...", "SCHEDULE A": "..."}
    findings: Annotated[list[Finding], operator.add]   # reducer merges parallel branches
    report_markdown: str


# ---------------------------------------------------------------------------
# 4. Ingestion helpers — Azure Document Intelligence markdown output
# ---------------------------------------------------------------------------
#
# Document Intelligence's "prebuilt-layout" model (output_content_format="markdown")
# turns the executed PDF/docx into GFM markdown: headings become '#'-prefixed
# lines, tables become GFM tables, and it inserts structural comments like
# <!-- PageBreak --> / <!-- PageNumber="3" -->. We strip the DI-specific
# comments and otherwise pass the markdown straight through — tables read
# fine as context for the LLM as-is.

DI_COMMENT_RE = re.compile(
    r"<!--\s*(PageBreak|PageNumber=.*?|PageFooter=.*?|PageHeader=.*?)\s*-->",
    re.IGNORECASE,
)


def extract_text(path: str) -> str:
    """Load markdown already produced by Azure Document Intelligence.

    If you're calling DI directly in the same pipeline instead of reading a
    saved .md file, swap this for the DI client call, e.g.:

        from azure.ai.documentintelligence import DocumentIntelligenceClient
        from azure.ai.documentintelligence.models import DocumentContentFormat
        poller = di_client.begin_analyze_document(
            "prebuilt-layout", body=f, output_content_format=DocumentContentFormat.MARKDOWN
        )
        text = poller.result().content
    """
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    return DI_COMMENT_RE.sub("", raw)


def extract_markdown_via_di(path: str, endpoint: str | None = None, key: str | None = None) -> str:
    """Run Azure Document Intelligence's prebuilt-layout model on a raw
    PDF/DOCX and return its markdown output, WITH the DI page-marker
    comments (<!-- PageNumber="N" -->, <!-- PageBreak -->) still intact.

    This is the real DI call the extract_text() docstring above describes —
    kept separate so extract_text() (used by the CLI entry point / anything
    that already has a pre-extracted .md) doesn't change behavior.

    Pass `endpoint`/`key` to target a specific Azure DI resource instance
    (used by extract_pages_parallel() to spread pages across a pool of
    instances). If omitted, falls back to the single configured resource:
        AZURE_DI_ENDPOINT   e.g. https://<resource>.cognitiveservices.azure.com
        AZURE_DI_KEY
    (swap for Entra ID auth the same way get_llm() above does, if preferred)
    """
    from azure.ai.documentintelligence import DocumentIntelligenceClient
    from azure.ai.documentintelligence.models import DocumentContentFormat
    from azure.core.credentials import AzureKeyCredential

    endpoint = endpoint or config.AZURE_DOC_INTELLIGENCE_ENDPOINT or os.environ.get("AZURE_DI_ENDPOINT")
    key = key or config.AZURE_DOC_INTELLIGENCE_KEY or os.environ.get("AZURE_DI_KEY")
    if not endpoint or not key:
        raise RuntimeError(
            "Azure Document Intelligence isn't configured — set AZURE_DI_ENDPOINT "
            "and AZURE_DI_KEY (or wire up Entra ID auth) before extracting a PDF/DOCX."
        )

    client = DocumentIntelligenceClient(endpoint=endpoint, credential=AzureKeyCredential(key))

    with open(path, "rb") as f:
        poller = client.begin_analyze_document(
            "prebuilt-layout",
            body=f,
            output_content_format=DocumentContentFormat.MARKDOWN,
        )

    result = poller.result()
    return result.content  # markdown, DI page-marker comments intact


# ---------------------------------------------------------------------------
# 4b. Multi-instance parallel extraction
# ---------------------------------------------------------------------------
#
# A single Azure DI resource defaults to 15 TPS (analyze-submit and result-poll
# are each capped separately). That's plenty for one page at a time, but
# submitting a whole multi-page contract's worth of pages at once can trip
# that ceiling and return HTTP 429. Spreading pages round-robin across a pool
# of DI resource instances raises the effective submit-side ceiling to
# `len(DI_INSTANCES) * 15` TPS.
#
# IMPORTANT — what this does and doesn't buy you:
#   - It removes the "N pages x per-page latency" penalty of a sequential
#     loop, and it avoids throttling on larger documents.
#   - It does NOT make any individual Azure DI call faster. Each analyze
#     call has its own network + OCR/layout-inference latency (commonly a
#     few seconds for prebuilt-layout, more for dense/scanned pages). That
#     per-call latency is a floor: with enough parallel capacity, the WHOLE
#     document's wall-clock time approaches that floor instead of summing
#     it across pages — it can't drop below it.

def _load_di_instances() -> list[dict]:
    """Build the pool of Azure Document Intelligence instances to spread
    per-page extraction calls across.

    Priority:
      1. config.AZURE_DI_INSTANCES = [{"endpoint": ..., "key": ...}, ...]
      2. AZURE_DI_ENDPOINTS / AZURE_DI_KEYS env vars — comma-separated,
         paired by position, e.g.:
           AZURE_DI_ENDPOINTS="https://di1...,https://di2...,https://di3..."
           AZURE_DI_KEYS="key1,key2,key3"
      3. Single AZURE_DI_ENDPOINT / AZURE_DI_KEY (existing single-resource setup)
    """
    configured = getattr(config, "AZURE_DI_INSTANCES", None)
    if configured:
        return list(configured)

    endpoints = config.AZURE_DOC_INTELLIGENCE_ENDPOINT or os.environ.get("AZURE_DI_ENDPOINTS")
    keys = config.AZURE_DOC_INTELLIGENCE_KEY or os.environ.get("AZURE_DI_KEYS")
    if endpoints and keys:
        ep_list = [e.strip() for e in endpoints.split(",") if e.strip()]
        key_list = [k.strip() for k in keys.split(",") if k.strip()]
        if ep_list and len(ep_list) == len(key_list):
            return [{"endpoint": e, "key": k} for e, k in zip(ep_list, key_list)]

    endpoint = config.AZURE_DOC_INTELLIGENCE_ENDPOINT or os.environ.get("AZURE_DI_ENDPOINT")
    key = config.AZURE_DOC_INTELLIGENCE_KEY or os.environ.get("AZURE_DI_KEY")
    if endpoint and key:
        return [{"endpoint": endpoint, "key": key}]

    return []


DI_INSTANCES: list[dict] = _load_di_instances()

# Conservative cap on concurrent in-flight analyze calls per instance.
# Azure DI's default (Standard/S0) tier allows 15 TPS each for submitting
# (POST) and polling (GET) analyze calls. Capping in-flight calls per
# instance below that leaves headroom for the poll traffic every in-flight
# analyze operation also generates. Override with AZURE_DI_CONCURRENCY_PER_INSTANCE.
DI_CONCURRENCY_PER_INSTANCE = int(os.environ.get("AZURE_DI_CONCURRENCY_PER_INSTANCE", "10"))
DI_TPS_PER_INSTANCE = int(os.environ.get("AZURE_DI_TPS_PER_INSTANCE", "15"))

_di_semaphores: dict[int, threading.Semaphore] = {
    i: threading.Semaphore(DI_CONCURRENCY_PER_INSTANCE) for i in range(len(DI_INSTANCES))
}


def extract_pages_parallel(
    page_paths: list[tuple[int, str]],
    on_page_done=None,
) -> dict[int, str]:
    """Run extract_markdown_via_di for each (page_num, single_page_pdf_path)
    concurrently, spreading calls round-robin across DI_INSTANCES.

    `on_page_done(page_num, instance_idx, markdown_or_None, error_or_None)`
    is called from the MAIN thread as each page finishes (via as_completed),
    so it's safe to update Streamlit UI elements from it.

    Returns {page_num: markdown} for pages that succeeded. Raises the first
    error encountered if every page failed.
    """
    if not DI_INSTANCES:
        raise RuntimeError(
            "Azure Document Intelligence isn't configured — set AZURE_DI_ENDPOINT/KEY "
            "(single instance) or AZURE_DI_ENDPOINTS/AZURE_DI_KEYS (comma-separated, "
            "multi-instance) before extracting a PDF/DOCX."
        )

    instance_cycle = cycle(range(len(DI_INSTANCES)))
    results: dict[int, str] = {}
    errors: dict[int, Exception] = {}

    def _call(pdf_path: str, instance_idx: int):
        instance = DI_INSTANCES[instance_idx]
        with _di_semaphores[instance_idx]:  # cap concurrent in-flight calls per instance
            return extract_markdown_via_di(pdf_path, instance["endpoint"], instance["key"])

    with ThreadPoolExecutor(max_workers=max(len(page_paths), 1)) as executor:
        future_to_page = {}
        for page_num, pdf_path in page_paths:
            instance_idx = next(instance_cycle)
            future = executor.submit(_call, pdf_path, instance_idx)
            future_to_page[future] = (page_num, instance_idx)

        for future in as_completed(future_to_page):
            page_num, instance_idx = future_to_page[future]
            try:
                markdown = future.result()
                results[page_num] = markdown
                if on_page_done:
                    on_page_done(page_num, instance_idx, markdown, None)
            except Exception as e:
                errors[page_num] = e
                if on_page_done:
                    on_page_done(page_num, instance_idx, None, e)

    if errors and not results:
        raise errors[min(errors)]

    return results


def recommend_di_instance_count(
    num_pages: int,
    target_seconds: float = 2.0,
    observed_per_call_seconds: float = 3.0,
    tps_per_instance: int = DI_TPS_PER_INSTANCE,
) -> dict:
    """Rough guidance for how many Azure DI instances are needed to extract
    `num_pages` pages in parallel without throttling.

    Azure DI's per-call latency (queue + OCR/layout inference — commonly a
    few seconds for prebuilt-layout, more for dense/scanned pages) is a
    FLOOR that adding instances cannot reduce; it's inherent to each analyze
    call. What more instances buy you is enough submit-side TPS headroom
    that all pages can be *in flight at once*, so the whole document's
    wall-clock time approaches that floor instead of summing across pages.
    """
    instances_for_tps = max(1, math.ceil(num_pages / max(tps_per_instance, 1)))
    return {
        "num_pages": num_pages,
        "instances_needed_for_tps": instances_for_tps,
        "tps_per_instance": tps_per_instance,
        "observed_per_call_seconds": observed_per_call_seconds,
        "target_seconds": target_seconds,
        "target_is_achievable": observed_per_call_seconds <= target_seconds,
    }


PAGE_MARKER_RE = re.compile(
    r"<!--\s*(?:PageNumber=.*?|PageBreak)\s*-->",
    re.IGNORECASE,
)


def split_markdown_by_page(markdown: str) -> list[str]:
    """Split DI markdown into one chunk per page using its own page-marker
    comments, stripping the markers themselves out of each chunk.

    Falls back to a single "page" (the whole text) if no markers are found —
    e.g. a plain .md upload that wasn't produced by DI.
    """
    chunks = [c.strip() for c in PAGE_MARKER_RE.split(markdown) if c.strip()]
    return chunks or [markdown.strip()]


def split_into_sections(text: str) -> dict:
    """Split on ARTICLE N / SCHEDULE X headings so each reviewer gets grounded context.

    Document Intelligence usually renders contract headings as markdown
    headings (e.g. '## ARTICLE 5 — SERVICES...'), but styling can vary by
    source formatting, so the pattern tolerates an optional leading run of '#'.
    """
    pattern = re.compile(
        r"(^#{0,6}\s*ARTICLE \d+.*$|^#{0,6}\s*SCHEDULE [A-Z].*$)",
        re.IGNORECASE | re.MULTILINE,
    )
    parts = pattern.split(text)
    sections = {}
    current = "PREAMBLE"
    for chunk in parts:
        if pattern.match(chunk or ""):
            current = chunk.strip().lstrip("#").strip()
            sections[current] = ""
        else:
            sections[current] = sections.get(current, "") + chunk
    return sections


# ---------------------------------------------------------------------------
# 5. Nodes
# ---------------------------------------------------------------------------

# One structured-output client per model actually used, built lazily and cached
# so a run that only touches Sonnet doesn't also spin up Opus/Haiku clients.
_structured_llm_cache: dict[str, object] = {}


def get_structured_llm(category: str):
    model = MODEL_ROUTING.get(category, DEFAULT_MODEL)
    if model not in _structured_llm_cache:
        _structured_llm_cache[model] = get_llm(model).with_structured_output(CategoryReview)
    return _structured_llm_cache[model]


def load_node(state: ReviewState) -> dict:
    sections = split_into_sections(state["doc_text"])
    return {"sections": sections}


def dispatch(state: ReviewState):
    """Fan out one Send per checklist category -> parallel reviewer_node calls."""
    return [
        Send("reviewer_node", {"category": cat, "sections": state["sections"]})
        for cat in CHECKLIST
    ]


def reviewer_node(payload: dict) -> dict:
    category = payload["category"]
    spec = CHECKLIST[category]
    doc_context = "\n\n".join(f"### {k}\n{v}" for k, v in payload["sections"].items())

    prompt = f"""You are {spec['persona']}, reviewing a Master Service Agreement for a
fleet management company (Wheels Inc.) before it goes to execution.

Review ONLY the checklist items below for the "{category}" category. For each item,
find the relevant clause(s) in the contract text, decide pass/flag/fail, and give a
concrete recommendation grounded in fleet-management industry norms (not generic legal
boilerplate advice).

Checklist items:
{chr(10).join(f"- {i}" for i in spec['items'])}

Contract text (grouped by Article/Schedule):
{doc_context}
"""
    result: CategoryReview = get_structured_llm(category).invoke(prompt)
    for f in result.findings:
        f.category = category
    return {"findings": result.findings}


def aggregate_node(state: ReviewState) -> dict:
    findings = state["findings"]
    risk_weight = {"low": 1, "medium": 3, "high": 5}
    score = sum(risk_weight.get(f.severity, 0) for f in findings if f.status != "pass")

    lines = ["# MSA Review Report — Wheels Inc.\n", f"**Overall risk score:** {score}\n"]
    for cat in CHECKLIST:
        cat_findings = [f for f in findings if f.category == cat]
        lines.append(f"\n## {cat.replace('_', ' ').title()}")
        for f in cat_findings:
            mark = {"pass": "✅", "flag": "⚠️", "fail": "❌"}.get(f.status, "?")
            lines.append(
                f"- {mark} **[{f.severity.upper()}] {f.checklist_item}** "
                f"(ref: {f.clause_reference})\n  {f.issue}\n  → *Recommendation:* {f.recommendation}"
            )
    return {"report_markdown": "\n".join(lines)}


# ---------------------------------------------------------------------------
# 6. Graph assembly
# ---------------------------------------------------------------------------

graph = StateGraph(ReviewState)
graph.add_node("load", load_node)
graph.add_node("reviewer_node", reviewer_node)
graph.add_node("aggregate", aggregate_node)

graph.add_edge(START, "load")
graph.add_conditional_edges("load", dispatch, ["reviewer_node"])
graph.add_edge("reviewer_node", "aggregate")
graph.add_edge("aggregate", END)

app = graph.compile()


# ---------------------------------------------------------------------------
# 7. Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "Wheels_Inc_Master_Service_Agreement.md"
    text = extract_text(path)
    result = app.invoke({"doc_text": text, "findings": []})
    print(result["report_markdown"])
    with open("review_report.md", "w",encoding="utf-8") as f:
        f.write(result["report_markdown"])
