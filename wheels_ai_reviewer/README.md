# Wheels AI Contract Reviewer

An AI-assisted contract review tool that reads a Master Service Agreement (or similar commercial agreement), runs it through a panel of specialist AI reviewers in parallel, and turns the result into a decision-ready report — with reviewer sign-off, a business-impact estimate, and export to PDF/DOCX.

## Purpose

Fleet management contracts (and commercial agreements generally) go through manual legal/ops review before execution — someone reads the whole document against a mental or written checklist, flags issues, and writes up findings. That process is slow, inconsistent between reviewers, and hard to audit after the fact.

This tool automates the first pass:

- **Extracts** the contract into structured text, page by page, via Azure Document Intelligence
- **Reviews** it against a fixed checklist, split across specialist categories (definitions & scope, billing & payment, liability & data, termination & boilerplate, etc.), each reviewed by its own AI persona
- **Scores** the contract's risk based on how many checklist items failed or were flagged, and how severe each is
- **Lets a human reviewer** accept or reject each AI recommendation with a reason — the AI proposes, a person still decides
- **Estimates business impact** (review time/cost saved, dollar risk surfaced) as an editable business case
- **Produces a final report**, downloadable as PDF or DOCX, with the dashboard, business impact, and every reviewer decision and reason attached

It's built specifically around a fleet-management MSA checklist today, but the extraction and section-splitting logic is generic to any agreement — see Architecture below.

## Architecture

```
┌─────────────┐     ┌──────────────────┐     ┌────────────────────────┐
│  Streamlit  │────▶│  Azure Document   │────▶│   LangGraph review      │
│  UI (app.py)│     │  Intelligence     │     │   pipeline               │
│             │     │  (N instances,    │     │   (msa_reviewer_agent)   │
│             │◀────│  pages in         │◀────│   (categories in        │
└─────────────┘     │  parallel → MD)   │     │   parallel via Send)     │
                     └──────────────────┘     └────────────────────────┘
                                                          │
                                                          ▼
                                              ┌────────────────────────┐
                                              │ Claude on Azure AI      │
                                              │ Foundry (Anthropic      │
                                              │ Messages API)           │
                                              └────────────────────────┘
```

### 1. UI layer — `app.py` (Streamlit)

A 5-step wizard, gated behind a login screen:

| Step | Page function | What happens |
|---|---|---|
| — | `login_page()` | Username/password gate (`config.USERNAME`/`config.PASSWORD`) |
| — | `dashboard()` | Landing screen — currently shows placeholder metrics/recent-reviews (not yet wired to real review history) |
| 1 | `upload_page()` | Accepts `.pdf`, `.docx`, or `.md` |
| 2 | `extraction_page()` | Runs Azure Document Intelligence extraction (see below) |
| 3 | `review_page()` | Shows every AI finding; reviewer sets **Accept/Reject** + a reason per item |
| 4 | `roi_page()` | Editable business-impact calculator |
| 5 | `report_page()` | Final dashboard + decisions + PDF/DOCX download |

All state (uploaded file, extracted text, findings, reviewer decisions, business-impact assumptions) lives in `st.session_state`, so the wizard can be navigated back and forth without losing work.

### 2. Extraction layer — Azure Document Intelligence (parallel, multi-instance)

For `.pdf` uploads, `extraction_page()`:
1. Splits the PDF into single-page files locally with `pypdf` (no network call)
2. Sends every page to Azure Document Intelligence's `prebuilt-layout` model **in parallel**, round-robined across a configurable pool of DI resource instances (`extract_pages_parallel()` in `msa_reviewer_agent.py`)
3. Reassembles the page markdown in order once all calls complete

**Why multiple instances, not just multiple threads against one instance:** a single Azure DI resource has a fixed request-rate ceiling (15 requests/sec by default on Standard/S0). Submitting every page of a large contract to one instance at once risks HTTP 429 throttling regardless of how much client-side concurrency you throw at it. `msa_reviewer_agent.py` solves this at the DI-resource level, not just the thread level:

- **Instance pool** (`DI_INSTANCES`) — configure more than one Azure DI resource via `AZURE_DI_ENDPOINTS` / `AZURE_DI_KEYS` (comma-separated, paired by position); falls back to a single instance if only one endpoint/key pair is set
- **Round-robin dispatch** (`extract_pages_parallel()`) — every page is assigned to the next instance in the pool via `itertools.cycle`, then all pages are fired concurrently with a `ThreadPoolExecutor`, capped at a configurable number of in-flight calls per instance (`DI_CONCURRENCY_PER_INSTANCE`, default 10) to leave headroom under each instance's TPS limit
- **Sizing guidance** (`recommend_di_instance_count()`) — given a page count, returns how many instances are needed to keep the whole batch's submissions under the combined TPS ceiling: `instances_needed = ceil(page_count / tps_per_instance)`. The `extraction_page()` UI surfaces this live — "Pages", "DI Instances Configured", and "Instances Recommended" — and warns if the configured pool is undersized for the document just uploaded
- **What this does and doesn't buy you:** wall-clock time for the whole document approaches a *single page's* DI latency (typically a few seconds) instead of summing that latency across every page — but it can't make any individual DI call faster than its own network + OCR/layout-inference time. More instances remove throttling on larger documents; they don't lower the per-call floor.

`.docx` uploads go through DI as a single call (pypdf can't split a docx into pages, so there's nothing to parallelize); `.md` uploads are treated as already-extracted text and skip DI entirely.

> Two standalone scripts, `doc_intelligence.py` and `pdf-page-manger.py`, are earlier prototypes of this same DI extraction logic — both call a single DI instance sequentially, page by page, with no parallelism. Neither is imported by `app.py`. The production path is `extract_markdown_via_di()` / `extract_pages_parallel()` in `msa_reviewer_agent.py`. Worth deleting or clearly marking as archived to avoid confusion about which extraction code is actually live.

### 3. Review layer — `msa_reviewer_agent.py` (LangGraph)

A LangGraph `StateGraph` with four nodes:

```
START → load → dispatch ──Send──▶ reviewer_node (× N categories, in parallel) → aggregate → END
```

- **`load_node`** splits the extracted markdown into sections by heading (`split_into_sections()` — splits on any markdown `#`–`######` heading, so it isn't tied to MSA-specific vocabulary like "ARTICLE"/"SCHEDULE" and generalizes to other agreement types)
- **`dispatch`** fans out one `Send` per checklist category defined in `CHECKLIST` (8 categories today: definitions & scope, onboarding & operations, service calculations, billing & payment, fees & adjustments, liability & data, termination & boilerplate, schedules & exhibits) — LangGraph runs these concurrently
- **`reviewer_node`** runs once per category, with its own persona/system prompt (e.g. "a fleet management billing analyst with deep expertise in fuel, mileage, toll..."), and returns structured findings via `with_structured_output(CategoryReview)` — each `Finding` has a `status` (pass/flag/fail) and `severity` (low/medium/high), constrained to that fixed vocabulary so scoring can't silently drop mismatched values
- **`aggregate_node`** computes the overall risk score (`Σ severity_weight` for every non-passing finding: low=1, medium=3, high=5) and renders the markdown report

The LLM calls go to Claude models deployed on Azure AI Foundry via `ChatAnthropic` pointed at Foundry's Anthropic-compatible endpoint (`get_llm()`), with per-category model routing available (`MODEL_ROUTING`) if some categories warrant a stronger/cheaper model than others.

### 4. Report generation — `report_utils.py`

Takes the same `report_data` dict and renders it two ways:
- `build_docx_report()` — python-docx
- `build_pdf_report()` — reportlab

Both include the dashboard (risk score, pass/flag/fail counts), the business-impact figures, and every reviewer decision with its reason — so the exported report doubles as an audit record of what the AI found *and* what a human did about it.

### 5. Configuration — `config.py`

Central place for branding (title, colors, logo), login credentials, Azure Document Intelligence endpoint/key, and the Azure AI Foundry model deployment endpoint/key/name. Currently checked into the repo with blank values — **these need to move to environment variables or a secrets manager before this goes anywhere near production**, since `config.py` as-is would put credentials in source control.

## Business Advantage

**Review cycle time.** A manual first-pass review of a multi-page MSA against a fixed checklist is inherently slow and serial. This tool parallelizes at two independent layers: extraction and review. Every page of the uploaded contract is sent to Azure Document Intelligence concurrently, spread across a configurable pool of DI resource instances so larger contracts don't hit a single instance's rate limit; every checklist category is then reviewed concurrently by LangGraph's `Send` fan-out. The combined effect is that total review time approaches the latency of *one* DI call plus *one* LLM call, not the sum of every page and every checklist category processed one at a time — and the DI instance pool can be scaled up (`recommend_di_instance_count()` tells you by how much) as contract length or review volume grows, rather than the pipeline slowing down linearly with page count.

**Consistency.** A checklist-driven, structured-output review means every contract gets checked against the same fixed set of items every time, by a reviewer "persona" tuned to that category — reducing the variance you get between different human reviewers' attention and expertise on any given day.

**Audit trail, not black-box automation.** The tool doesn't auto-decide anything — every AI finding requires an explicit human Accept/Reject with a typed reason before it reaches the final report. That reason is preserved in the exported PDF/DOCX. For a compliance-sensitive process like contract execution, that's the difference between "the AI approved this" (a liability) and "the AI flagged this, and here's the documented human judgment call" (a defensible record).

**Quantifiable business case.** The ROI page turns "we bought an AI reviewer" into a number: hours and dollars saved per contract (and annualized across your review volume) versus a manual baseline, plus the dollar exposure represented by whatever the AI flagged. The inputs are editable assumptions, not hardcoded claims — so the business case can be grounded in your actual reviewer rates and historical issue costs rather than generic placeholders.

**Generalizes past this one contract type.** Because section-splitting works off document structure (markdown headings) rather than fleet/MSA-specific keywords, extending this to other agreement types (NDAs, SOWs, leases) is mostly a matter of writing a new `CHECKLIST`, not rebuilding the pipeline.

## Known Gaps / Before This Is "Enterprise-Ready"

Being direct about what's still a prototype, since a README that oversells this would undercut its own credibility:

- **Credentials in `config.py`** — move to environment variables/secrets manager; don't ship blank placeholders in source control as the pattern.
- **Dashboard is placeholder data** — `dashboard()` shows hardcoded metrics (128 contracts reviewed, a static 3-row table), not real review history. There's no persistence layer yet; every review lives only in that session's `st.session_state`.
- **Single-user login** — one username/password pair in `config.py`, no per-user accounts, roles, or audit-by-user.
- **Model determinism** — temperature and structured-output vocabulary are pinned to reduce run-to-run drift, but Claude API calls aren't byte-for-byte deterministic; identical reruns on the same document can still produce minor wording/severity differences.
- **Legacy extraction scripts** (`doc_intelligence.py`, `pdf-page-manger.py`) aren't wired into the app and should be archived or removed to avoid confusion with the live extraction path.

## Requirements

```
streamlit
pypdf
langchain-anthropic
langgraph
pydantic
azure-ai-documentintelligence
azure-core
python-docx
reportlab
```

## Running It

```bash
pip install -r requirements.txt   # see Requirements above
streamlit run app.py
```

Set the following in `config.py` (or, preferably, environment variables) before running:
- `AZURE_DOC_INTELLIGENCE_ENDPOINT`, `AZURE_DOC_INTELLIGENCE_KEY`
- `DEPLOYMENT_ENDPOINT`, `DEPLOYMENT_KEY`, `DEPLOYMENT_NAME` (Azure AI Foundry Claude deployment)
- `USERNAME`, `PASSWORD` (app login)

For parallel multi-instance document extraction, also set `AZURE_DI_ENDPOINTS` / `AZURE_DI_KEYS` (comma-separated, paired by position) to spread page extraction across more than one Azure DI resource.
