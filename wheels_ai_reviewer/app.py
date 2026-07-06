import streamlit as st
# import os  # not used anywhere in this file — remove if still unused later
from pathlib import Path
import time  # used for elapsed-time display during parallel DI extraction
import tempfile
import hashlib
from datetime import datetime
from itertools import cycle
from concurrent.futures import ThreadPoolExecutor, as_completed
from pypdf import PdfReader, PdfWriter  # used to split PDFs into per-page files for real per-page DI calls

from msa_reviewer_agent import (
    app as review_graph,
    extract_text,
    extract_markdown_via_di,
    extract_pages_parallel,
    recommend_di_instance_count,
    DI_INSTANCES,
    # split_markdown_by_page,  # not used — pages are split with pypdf BEFORE calling DI now
    CHECKLIST,
)
from report_utils import build_docx_report, build_pdf_report
import db as reviewdb
import plotly.express as px

# ------------------------------------------
# Configuration
# ------------------------------------------

st.set_page_config(
    page_title="Wheels AI Contract Reviewer",
    page_icon="🚗",
    layout="wide"
)

USERNAME = "admin"
PASSWORD = "admin"

# ------------------------------------------
# Session State
# ------------------------------------------

if "logged_in" not in st.session_state:
    st.session_state.logged_in = False

if "page" not in st.session_state:
    st.session_state.page = "dashboard"

if "wizard_step" not in st.session_state:
    st.session_state.wizard_step = 1

if "decisions" not in st.session_state:
    # Keyed by "{category}__{checklist_item}" -> {decision: Pending|Accept|Reject, reason: str, ...finding fields}
    st.session_state.decisions = {}

if "business_impact" not in st.session_state:
    st.session_state.business_impact = {}

if "saved_review_id" not in st.session_state:
    st.session_state.saved_review_id = None  # set once this review is persisted to review_history.db

reviewdb.init_db()  # idempotent — creates review_history.db on first run only

# ------------------------------------------
# Navigation
# ------------------------------------------

# PAGES map is currently unused (st.session_state.page drives routing directly
# at the bottom of the file). Left here commented in case a nav menu is added later.
# PAGES = {
#     "dashboard": "Dashboard",
#     "upload": "Upload",
#     "extraction": "Extraction",
#     "review": "Review",
#     "roi": "Business Value",
#     "report": "Report"
# }

# ------------------------------------------
# Styling
# ------------------------------------------

st.markdown("""
<style>

.stApp{
    background: var(--background-color);
}

.main-title{
    font-size:42px;
    font-weight:700;
    color:#2F6FED;
}

.sub-title{
    color: var(--text-color);
    opacity: 0.7;
    margin-bottom:30px;
}

.metric-card{
    background: var(--secondary-background-color);
    color: var(--text-color);
    padding:20px;
    border-radius:10px;
    border:1px solid rgba(128,128,128,0.25);
}

.login-card{

    max-width:500px;

    margin:auto;

    background: var(--secondary-background-color);

    color: var(--text-color);

    padding:40px;

    border-radius:15px;

    border:1px solid rgba(128,128,128,0.25);

    box-shadow:0px 3px 15px rgba(0,0,0,.15);

}

</style>
""", unsafe_allow_html=True)

# ------------------------------------------
# Login Screen
# ------------------------------------------


def login_page():

    st.markdown(
        "<h1 class='main-title'>🚗 Wheels AI</h1>",
        unsafe_allow_html=True
    )

    st.markdown(
        "<p class='sub-title'>Fleet Contract Intelligence Platform</p>",
        unsafe_allow_html=True
    )

    col1, col2, col3 = st.columns([2,3,2])

    with col2:

        with st.container(border=True):

            st.subheader("Login")

            username = st.text_input("Username")

            password = st.text_input(
                "Password",
                type="password"
            )

            if st.button(
                "Login",
                use_container_width=True
            ):

                if username == USERNAME and password == PASSWORD:

                    st.session_state.logged_in = True
                    st.rerun()

                else:

                    st.error("Invalid username or password")

#             st.divider()

#             st.caption("Demo Credentials")

#             st.code(
# """Username : admin

# Password : admin"""
#             )

    # Decorative graphic anchored at the bottom of the login page
    try:
        with open("assets/login_wave.svg", "r") as f:
            login_wave_svg = f.read()
        st.markdown(login_wave_svg, unsafe_allow_html=True)
    except FileNotFoundError:
        pass  # decorative only — don't break the login page if the asset is missing

# ------------------------------------------
# Wizard
# ------------------------------------------


# Maps each wizard step number to the page that renders it — used by the
# back button in wizard() to know which page to return to.
STEP_PAGES = {
    1: "upload",
    2: "extraction",
    3: "ai_review",
    4: "review",
    5: "roi",
    6: "report",
}


def wizard():

    steps = [

        "Upload",

        "Extraction",

        "AI Review",

        "Manual Review",

        "ROI",

        "Report"

    ]

    nav_col, steps_col = st.columns([0.6, 11.4])

    with nav_col:

        if st.session_state.wizard_step > 1:

            if st.button("⬅", key="wizard_back", help="Back to previous step"):

                st.session_state.wizard_step -= 1

                st.session_state.page = STEP_PAGES[st.session_state.wizard_step]

                st.rerun()

    with steps_col:

        cols = st.columns(len(steps))

        for i, col in enumerate(cols):

            with col:

                if i + 1 == st.session_state.wizard_step:

                    st.success(f"{i+1}. {steps[i]}")

                else:

                    st.info(f"{i+1}. {steps[i]}")

def sidebar():

    with st.sidebar:

        st.image(
            "https://img.icons8.com/color/96/truck.png",
            width=70
        )

        st.title("Wheels AI")

        st.caption("Fleet Contract Intelligence")

        st.divider()

        if st.button("🏠 Dashboard", use_container_width=True):

            st.session_state.page = "dashboard"

            st.rerun()

        if st.button("📄 New Review", use_container_width=True):

            st.session_state.page = "upload"

            st.session_state.wizard_step = 1

            st.rerun()

        st.divider()

        if st.button("Logout"):

            st.session_state.logged_in = False

            st.session_state.page = "dashboard"

            st.rerun()

# ------------------------------------------
# Dashboard
# ------------------------------------------


def dashboard():

    st.title("Fleet Contract Intelligence")

    st.caption("Welcome Rajan")

    wizard()

    st.write("")

    total_reviews = reviewdb.get_review_count()

    if total_reviews == 0:

        st.info("No reviews yet — upload a contract to get started.")

        return

    avg_score = reviewdb.get_average_score()

    tier_counts = reviewdb.get_risk_tier_distribution()

    most_common_tier = max(tier_counts, key=tier_counts.get) if tier_counts else "—"

    c1, c2, c3 = st.columns(3)

    c1.metric(
        "Contracts Reviewed",
        total_reviews
    )

    c2.metric(
        "Average Risk Score",
        avg_score
    )

    c3.metric(
        "Most Common Risk Tier",
        most_common_tier
    )

    st.divider()

    col_chart, col_table = st.columns([1, 1.4])

    with col_chart:

        st.subheader("Risk Distribution")

        tier_order = ["Low Risk", "Moderate Risk", "High Risk", "Critical Risk"]
        tier_colors = {
            "Low Risk": "#1E7A5F",
            "Moderate Risk": "#C9A227",
            "High Risk": "#D97B29",
            "Critical Risk": "#A3312A",
        }
        labels = [t for t in tier_order if tier_counts.get(t)]
        values = [tier_counts[t] for t in labels]

        fig = px.pie(
            names=labels,
            values=values,
            color=labels,
            color_discrete_map=tier_colors,
            hole=0.45,
        )
        fig.update_traces(textinfo="label+value")
        fig.update_layout(showlegend=False, margin=dict(l=10, r=10, t=10, b=10), height=320)

        st.plotly_chart(fig, use_container_width=True)

    with col_table:

        st.subheader("Recent Reviews")

        recent = reviewdb.get_recent_reviews(limit=10)

        st.dataframe(
            [
                {
                    "File Name": r["contract_name"],
                    "Score": r["risk_score"],
                    "Risk Tier": r["risk_tier"],
                    "Reviewed": r["reviewed_at"][:16].replace("T", " "),
                }
                for r in recent
            ],
            use_container_width=True,
            hide_index=True,
        )

    st.divider()

    st.subheader("Download Past Reports")

    st.caption("Re-download a previously generated report without re-running the review.")

    recent = reviewdb.get_recent_reviews(limit=10)

    for r in recent:

        rcol1, rcol2, rcol3, rcol4 = st.columns([3, 1.2, 1, 1])

        rcol1.write(r["contract_name"])

        rcol2.write(r["risk_tier"])

        reports = reviewdb.get_review_reports(r["id"])

        file_stub = Path(r["contract_name"]).stem or "MSA_Review"

        rcol3.download_button(
            "DOCX",
            data=reports["docx_report"],
            file_name=f"{file_stub}_Review_Report.docx",
            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            key=f"dash_docx_{r['id']}",
            use_container_width=True,
        )

        rcol4.download_button(
            "PDF",
            data=reports["pdf_report"],
            file_name=f"{file_stub}_Review_Report.pdf",
            mime="application/pdf",
            key=f"dash_pdf_{r['id']}",
            use_container_width=True,
        )

def upload_page():

    st.title("Step 1")

    st.subheader("Upload Master Service Agreement")

    wizard()

    st.write("")

    uploaded = st.file_uploader(
        "Upload Contract",
        type=["docx", "md", "pdf"]
    )

    if uploaded:

        suffix = Path(uploaded.name).suffix

        raw_bytes = uploaded.read()

        if suffix.lower() == ".md":

            # .md files are plain text, so normalize to valid UTF-8 up front.
            # This is the root-cause fix for:
            #   UnicodeDecodeError: 'utf-8' codec can't decode byte 0xe2 ...
            # which happens when a file saved in another encoding (e.g. Windows-1252,
            # common from Word/Outlook paste) is later opened with strict utf-8
            # somewhere downstream (previews, extract_text, etc.).
            try:

                text = raw_bytes.decode("utf-8")

            except UnicodeDecodeError:

                try:

                    text = raw_bytes.decode("cp1252")

                except UnicodeDecodeError:

                    text = raw_bytes.decode("utf-8", errors="replace")

            raw_bytes = text.encode("utf-8")

        contract_hash = hashlib.md5(raw_bytes).hexdigest()

        if st.session_state.get("contract_hash") != contract_hash:

            # BUG this fixes: page_markdowns/doc_text/review_findings/
            # review_complete/decisions/business_impact were only ever set
            # ONCE and never cleared on a new upload. So uploading contract B
            # after finishing contract A's review skipped extraction and the
            # AI review entirely, silently reusing contract A's cached
            # results — "the older one shows". Clearing them here, only when
            # the uploaded file's content actually changed (not on every
            # incidental rerun of this page), makes each new document a
            # clean run without also wiping in-progress state if the user
            # just navigates back to Step 1 with the same file still selected.
            for key in (
                "page_markdowns", "doc_text",
                "review_started", "review_complete", "review_findings",
            ):
                st.session_state.pop(key, None)

            st.session_state.decisions = {}
            st.session_state.business_impact = {}
            st.session_state.saved_review_id = None

        st.session_state.contract_hash = contract_hash

        with tempfile.NamedTemporaryFile(
            suffix=suffix,
            delete=False
        ) as tmp:

            tmp.write(raw_bytes)

            st.session_state.contract_path = tmp.name

            st.session_state.contract_name = uploaded.name

        st.success("Contract uploaded successfully")

        st.write("")

        col1, col2 = st.columns(2)

        with col1:

            st.metric(

                "File Name",

                uploaded.name

            )

            st.metric(

                "Size",

                f"{uploaded.size/1024:.1f} KB"

            )

        with col2:

            ext = Path(uploaded.name).suffix.lower()

            contract = "Unknown"

            if ext == ".docx":

                contract = "Master Service Agreement"

            elif ext == ".pdf":

                contract = "Scanned Contract"

            elif ext == ".md":

                contract = "Azure DI Markdown"

            st.metric(

                "Detected",

                contract

            )

            st.metric(

                "Confidence",

                "99%"

            )

        st.divider()

        st.subheader("Document Preview")

        if uploaded.name.endswith(".md"):

            # contract_path now holds the already UTF-8-normalized content
            # written above — read that back instead of the upload stream,
            # which has already been consumed by uploaded.read() earlier.
            with open(st.session_state.contract_path, "r", encoding="utf-8") as f:

                text = f.read()

            st.text_area(

                "",

                text[:5000],

                height=300

            )

        else:

            st.info("Preview available after Azure Document Intelligence extraction.")

        c1, c2 = st.columns([1, 1])

        with c1:

            if st.button("⬅ Dashboard", use_container_width=True):

                st.session_state.page = "dashboard"

                st.rerun()

        with c2:

            if st.button("Continue ➜", type="primary", use_container_width=True):

                st.session_state.page = "extraction"

                st.session_state.wizard_step = 2

                st.rerun()

    else:

        st.info("Waiting for document upload.")


def extraction_page():

    wizard()

    st.title("Step 2")

    contract_path = st.session_state.contract_path

    ext = Path(contract_path).suffix.lower()

    # ------------------------------------------------------------
    # Phase 1 — Document Extraction
    # For .md uploads: content is already DI-produced markdown — just load it.
    # For .pdf uploads: split into individual pages with pypdf, then call
    #   Azure DI (extract_markdown_via_di) separately for EACH page, updating
    #   that page's status in the UI the moment its own call returns. This
    #   is real per-page progress, not a single call revealed afterward.
    # For .docx: pypdf can't split a docx, so it's extracted as one page.
    # Runs once per document; cached in session_state so later re-runs
    # (button clicks below) don't repeat the extraction.
    # ------------------------------------------------------------
    if "page_markdowns" not in st.session_state:

        st.subheader("AI Document Extraction")

        page_markdowns = []

        if ext == ".md":

            st.caption("Loading pre-extracted Azure Document Intelligence markdown")

            status = st.empty()

            status.info("📄 Loading markdown...")

            full_markdown = extract_text(contract_path)

            status.success("✅ Markdown loaded")

            page_markdowns = [{"page": 1, "markdown": full_markdown}]

        elif ext == ".pdf":

            reader = PdfReader(contract_path)

            total_pages = len(reader.pages)

            st.caption(
                f"Azure Document Intelligence — extracting {total_pages} page(s) in "
                f"parallel across {max(len(DI_INSTANCES), 1)} instance(s)"
            )

            if not DI_INSTANCES:

                st.error(
                    "Azure Document Intelligence isn't configured — set AZURE_DI_ENDPOINT/KEY "
                    "(single instance) or AZURE_DI_ENDPOINTS/AZURE_DI_KEYS (comma-separated, "
                    "multi-instance) before extracting a PDF."
                )

                st.stop()

            # ------------------------------------------------------------
            # Show current setup vs. what's recommended for this document,
            # so throttling risk is visible before the calls go out.
            # ------------------------------------------------------------
            rec = recommend_di_instance_count(total_pages)

            with st.expander("⚡ Parallel extraction setup", expanded=True):

                c1, c2, c3 = st.columns(3)

                c1.metric("Pages", total_pages)

                c2.metric("DI Instances Configured", len(DI_INSTANCES))

                c3.metric("Instances Recommended", rec["instances_needed_for_tps"])

                if len(DI_INSTANCES) < rec["instances_needed_for_tps"]:

                    st.warning(
                        f"With {len(DI_INSTANCES)} instance(s) configured and Azure's "
                        f"default 15 analyze-requests/sec limit per instance, submitting "
                        f"all {total_pages} pages at once risks HTTP 429 throttling. "
                        f"Add {rec['instances_needed_for_tps'] - len(DI_INSTANCES)} more "
                        f"instance(s) for this document size, or extraction will queue "
                        f"within the existing instances instead (slower, still parallel)."
                    )

                st.caption(
                    "Wall-clock time for the whole document is bounded by Azure's "
                    "per-page latency (commonly 2-6s for prebuilt-layout, depending on "
                    "page density), not by page count — more instances remove "
                    "throttling on larger documents, they don't shrink a single call "
                    "below that floor."
                )

            # ------------------------------------------------------------
            # Split every page out up front (fast, local, no network) so
            # ALL pages can be submitted to Azure DI at the same time.
            # ------------------------------------------------------------
            page_paths = []

            for i in range(total_pages):

                writer = PdfWriter()

                writer.add_page(reader.pages[i])

                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp_page:

                    writer.write(tmp_page)

                page_paths.append((i + 1, tmp_page.name))

            page_status = {}

            status_cols = st.columns(2)

            for page_num, _ in page_paths:

                container = status_cols[(page_num - 1) % 2]

                page_status[page_num] = container.empty()

                page_status[page_num].info(f"⏳ Page {page_num} of {total_pages} — queued")

            progress = st.progress(0, text=f"Submitting {total_pages} pages to Azure DI in parallel...")

            timer_box = st.empty()

            for page_num, _ in page_paths:

                page_status[page_num].info(f"📄 Page {page_num} of {total_pages} — extracting (parallel)...")

            # ------------------------------------------------------------
            # Fire all pages at once, round-robin across DI_INSTANCES.
            # on_page_done fires on the MAIN thread (via as_completed inside
            # extract_pages_parallel), so it's safe to update these
            # placeholders directly from it.
            # ------------------------------------------------------------
            start_time = time.time()

            completed_count = {"n": 0}

            def _on_page_done(page_num, instance_idx, markdown, err):

                completed_count["n"] += 1

                elapsed = time.time() - start_time

                if err is not None:

                    page_status[page_num].error(f"❌ Page {page_num} of {total_pages} — failed: {err}")

                else:

                    page_status[page_num].success(
                        f"✅ Page {page_num} of {total_pages} — done via instance "
                        f"{instance_idx + 1} ({elapsed:.1f}s elapsed)"
                    )

                progress.progress(
                    completed_count["n"] / total_pages,
                    text=f"{completed_count['n']}/{total_pages} pages extracted ({elapsed:.1f}s elapsed)"
                )

                timer_box.caption(f"⏱ {elapsed:.1f}s elapsed")

            try:

                page_markdown_map = extract_pages_parallel(page_paths, on_page_done=_on_page_done)

            except RuntimeError as e:

                st.error(str(e))

                st.stop()

            total_elapsed = time.time() - start_time

            failed_pages = sorted(set(p for p, _ in page_paths) - set(page_markdown_map))

            if failed_pages:

                st.error(f"Azure DI call failed on page(s) {failed_pages} — see status above for details.")

                st.stop()

            st.success(
                f"✅ All {total_pages} pages extracted in {total_elapsed:.1f}s "
                f"using {len(DI_INSTANCES)} instance(s)"
            )

            page_markdowns = [
                {"page": p, "markdown": page_markdown_map[p]}
                for p in sorted(page_markdown_map)
            ]

        else:

            # .docx — pypdf can't split this, so it's one Azure DI call
            # covering the whole document (shown as a single "page").
            st.caption("Azure Document Intelligence — extracting document to Markdown")

            status = st.empty()

            status.info("📄 Sending document to Azure Document Intelligence...")

            try:

                full_markdown = extract_markdown_via_di(contract_path)

            except RuntimeError as e:

                status.error("❌ Extraction failed")

                st.error(str(e))

                st.stop()

            status.success("✅ Document analyzed by Azure Document Intelligence")

            page_markdowns = [{"page": 1, "markdown": full_markdown}]

        st.session_state.page_markdowns = page_markdowns

        st.session_state.doc_text = "\n\n".join(p["markdown"] for p in page_markdowns)

    page_markdowns = st.session_state.page_markdowns

    doc_text = st.session_state.doc_text

    st.success(f"✅ Extraction complete — {len(page_markdowns)} page(s) converted to Markdown")

    with st.expander("📄 View Extracted Markdown", expanded=False):

        tabs = st.tabs([f"Page {p['page']}" for p in page_markdowns])

        for tab, p in zip(tabs, page_markdowns):

            with tab:

                preview = p["markdown"][:5000]

                st.markdown(preview + ("..." if len(p["markdown"]) > 5000 else ""))

    st.divider()

    if st.button("Continue to AI Review ➜", type="primary", use_container_width=True):

        st.session_state.page = "ai_review"

        st.session_state.wizard_step = 3

        st.rerun()

# ------------------------------------------
# AI Review
# ------------------------------------------
#
# Runs the automated checklist review against the extracted document text.
# Kept as its own wizard step (separate from Extraction and from the
# reviewer's manual Accept/Reject pass on the following step).


def ai_review_page():

    wizard()

    st.title("Step 3")

    doc_text = st.session_state.doc_text

    # ------------------------------------------------------------
    # Phase 2 — AI Contract Review
    # Only runs once the user explicitly clicks "Start Review".
    # ------------------------------------------------------------
    st.subheader("AI Contract Review")

    review_done = st.session_state.get("review_complete", False)

    review_running = st.session_state.get("review_started", False)

    if not review_done and not review_running:

        st.info("Extraction is complete. Click below to run the AI review.")

        if st.button("▶ Start Review", type="primary"):

            st.session_state.review_started = True

            st.rerun()

        return  # wait here until the user triggers the review

    # Per-category status boxes (pending -> in progress via reruns -> done)
    reviewers = {}

    cols = st.columns(2)

    left, right = cols[0], cols[1]

    categories = list(CHECKLIST.keys())

    for i, category in enumerate(categories):

        container = left if i % 2 == 0 else right

        reviewers[category] = container.empty()

        reviewers[category].info(f"⏳ {category.replace('_',' ').title()}")

    progress = st.progress(0, text="Starting review...")

    status = st.empty()

    if not review_done:

        total = len(CHECKLIST)

        completed = 0

        findings = []

        for update in review_graph.stream(

            {

                "doc_text": doc_text,

                "findings": []

            },

            stream_mode="updates"

        ):

            for node, payload in update.items():

                if node != "reviewer_node":

                    continue

                if not payload.get("findings"):

                    continue

                finding = payload["findings"][0]

                findings.extend(payload["findings"])

                reviewers[finding.category].success(
                    f"✅ {finding.category.replace('_',' ').title()}"
                )

                completed += 1

                progress.progress(
                    completed / total,
                    text=f"{completed}/{total} Reviewers Complete"
                )

                status.info(f"Finished {finding.category}")

        st.session_state.review_findings = findings

        st.session_state.review_complete = True

    else:

        # Already ran — show everything as done immediately.
        for category in categories:

            reviewers[category].success(f"✅ {category.replace('_',' ').title()}")

        progress.progress(1.0, text=f"{len(categories)}/{len(categories)} Reviewers Complete")

        status.success("Review already complete")

    st.success("✅ Review Complete")

    # ------------------------------------------------------------
    # Final review output — shown inline at the end of this step
    # ------------------------------------------------------------
    findings = st.session_state.review_findings

    risk = {"low": 1, "medium": 3, "high": 5}

    score = sum(risk.get(f.severity, 0) for f in findings if f.status != "pass")

    c1, c2, c3, c4 = st.columns(4)

    c1.metric("Risk Score", score)

    c2.metric("Failures", len([x for x in findings if x.status == "fail"]))

    c3.metric("Flags", len([x for x in findings if x.status == "flag"]))

    c4.metric("Passed", len([x for x in findings if x.status == "pass"]))

    for category in categories:

        with st.expander(category.replace("_", " ").title(), expanded=False):

            rows = [x for x in findings if x.category == category]

            if not rows:

                st.caption("No findings for this category.")

                continue

            for item in rows:

                icon = {"pass": "✅", "flag": "⚠", "fail": "❌"}[item.status]

                st.markdown(f"**{icon} {item.checklist_item}**")

                st.caption(f"Severity: {item.severity} • Clause: {item.clause_reference}")

                st.write(item.issue)

                st.info(item.recommendation)

                st.divider()

    if st.button("View Full Review ➜", type="primary"):

        st.session_state.page = "review"

        st.session_state.wizard_step = 4

        st.rerun()

def review_page():

    wizard()

    st.title("Step 4")

    st.subheader("Manual Review")

    findings = st.session_state.review_findings

    risk = {

        "low":1,

        "medium":3,

        "high":5

    }

    score = sum(

        risk.get(f.severity,0)

        for f in findings

        if f.status!="pass"

    )

    c1,c2,c3,c4 = st.columns(4)

    c1.metric("Risk Score",score)

    c2.metric(

        "Failures",

        len([x for x in findings if x.status=="fail"])

    )

    c3.metric(

        "Flags",

        len([x for x in findings if x.status=="flag"])

    )

    c4.metric(

        "Passed",

        len([x for x in findings if x.status=="pass"])

    )

    st.divider()

    # ------------------------------------------------------------
    # Shared status -> icon/color lookup (used both for filtering and
    # for the colored marks on collapsed category/finding headers below).
    # NOTE: the ":color[...]" markdown syntax used in labels below needs
    # a reasonably recent Streamlit (1.31+). If it renders as literal text
    # on your Streamlit version, swap it for st.expander(..., icon=...)
    # instead (1.32+), or drop the color and keep just the emoji.
    # ------------------------------------------------------------
    STATUS_META = {

        "pass": {"icon": "✅", "label": "Pass", "color": "green"},

        "flag": {"icon": "⚠️", "label": "Flag", "color": "orange"},

        "fail": {"icon": "❌", "label": "Fail", "color": "red"},

    }

    STATUS_RANK = {"fail": 3, "flag": 2, "pass": 1}

    # ------------------------------------------------------------
    # Filters — status, severity, category
    # ------------------------------------------------------------
    st.markdown("#### Filter Findings")

    fcol1, fcol2, fcol3 = st.columns(3)

    with fcol1:

        status_filter = st.multiselect(

            "Status",

            options=list(STATUS_META.keys()),

            default=list(STATUS_META.keys()),

            format_func=lambda s: f"{STATUS_META[s]['icon']} {STATUS_META[s]['label']}",

        )

    with fcol2:

        severity_filter = st.multiselect(

            "Severity",

            options=["low", "medium", "high"],

            default=["low", "medium", "high"],

            format_func=lambda s: s.title(),

        )

    with fcol3:

        category_filter = st.multiselect(

            "Category",

            options=list(CHECKLIST.keys()),

            default=list(CHECKLIST.keys()),

            format_func=lambda c: c.replace("_", " ").title(),

        )

    filtered = [

        f for f in findings

        if f.status in status_filter

        and f.severity in severity_filter

        and f.category in category_filter

    ]

    st.caption(f"Showing {len(filtered)} of {len(findings)} findings")

    st.divider()

    # ------------------------------------------------------------
    # Per-category sections — collapsed by default, header shows a
    # color-coded mark for the worst status in that category plus a
    # pass/flag/fail count, so you can scan status without expanding.
    # Auto-expands only if the category has a flag/fail in it.
    # ------------------------------------------------------------
    for category in CHECKLIST:

        rows = [x for x in filtered if x.category == category]

        if not rows:

            continue  # nothing in this category matches the current filter

        worst_status = max(rows, key=lambda x: STATUS_RANK.get(x.status, 0)).status

        meta = STATUS_META[worst_status]

        pass_n = len([x for x in rows if x.status == "pass"])

        flag_n = len([x for x in rows if x.status == "flag"])

        fail_n = len([x for x in rows if x.status == "fail"])

        header = (

            f":{meta['color']}[{meta['icon']}] "

            f"**{category.replace('_',' ').title()}**  "

            f"— ✅ {pass_n}  ⚠️ {flag_n}  ❌ {fail_n}"

        )

        with st.expander(

            header,

            expanded=(worst_status != "pass")

        ):

            for item in rows:

                item_meta = STATUS_META[item.status]

                st.markdown(

                    f":{item_meta['color']}[{item_meta['icon']}] "

                    f"**{item.checklist_item}**"

                )

                st.write(

                    "**Severity:**",

                    item.severity.title()

                )

                st.write(

                    "**Clause:**",

                    item.clause_reference

                )

                st.write(

                    item.issue

                )

                st.info(

                    item.recommendation

                )

                # ------------------------------------------------------------
                # Decision + reason capture. Keyed on category+checklist item
                # so decisions persist in session_state across reruns/filters.
                # ------------------------------------------------------------
                decision_id = f"{item.category}__{item.checklist_item}"

                existing = st.session_state.decisions.get(
                    decision_id,
                    {"decision": "Pending", "reason": ""}
                )

                decision = st.radio(
                    "Decision",
                    ["Pending", "Accept", "Reject"],
                    index=["Pending", "Accept", "Reject"].index(existing["decision"]),
                    key=f"decision_{decision_id}",
                    horizontal=True,
                )

                reason = existing.get("reason", "")

                if decision == "Reject":

                    reason = st.text_area(
                        "Reason for declining this recommendation",
                        value=reason,
                        key=f"reason_{decision_id}",
                        placeholder="e.g. Business already has a mitigating control in place / Client won't accept this change / Out of scope for this cycle...",
                    )

                    if not reason.strip():

                        st.warning("Add a reason so it's captured in the final report.")

                elif decision == "Accept":

                    reason = st.text_area(
                        "Notes (optional)",
                        value=reason,
                        key=f"reason_{decision_id}",
                        placeholder="Optional context for the final report...",
                    )

                else:

                    reason = ""

                st.session_state.decisions[decision_id] = {
                    "category": item.category,
                    "checklist_item": item.checklist_item,
                    "status": item.status,
                    "severity": item.severity,
                    "clause_reference": item.clause_reference,
                    "issue": item.issue,
                    "recommendation": item.recommendation,
                    "decision": decision,
                    "reason": reason,
                }

                st.divider()

    if st.button(

        "Business Impact",

        type="primary"

    ):

        st.session_state.page="roi"

        st.session_state.wizard_step=5

        st.rerun()

# ------------------------------------------
# Business Impact
# ------------------------------------------
#
# Translates the AI review findings into a rough dollar/time business case:
#   - Review-cycle time & cost saved vs. a manual review (per contract + annualized)
#   - Dollar exposure surfaced by flagged/failed findings (issues caught before execution)
# All dollar/time inputs are editable assumptions — these are directional
# estimates for a business case, not audited figures.


def roi_page():

    wizard()

    st.title("Step 5")

    st.subheader("Business Impact")

    findings = st.session_state.get("review_findings", [])

    total_items = len(findings)

    st.caption(
        "Adjust the assumptions below to match your organization — the estimates "
        "update automatically. These are directional figures for a business case, "
        "not audited numbers."
    )

    with st.expander("⚙️ Assumptions", expanded=True):

        c1, c2, c3 = st.columns(3)

        with c1:

            minutes_per_item_manual = st.number_input(
                "Manual review minutes / checklist item",
                min_value=1,
                value=20,
            )

            hourly_rate = st.number_input(
                "Blended reviewer hourly rate ($)",
                min_value=10,
                value=150,
            )

        with c2:

            ai_minutes_total = st.number_input(
                "AI review time for whole contract (minutes)",
                min_value=1,
                value=8,
            )

            contracts_per_year = st.number_input(
                "Similar contracts reviewed per year",
                min_value=1,
                value=50,
            )

        with c3:

            risk_dollar_low = st.number_input(
                "Exposure per LOW severity issue ($)",
                min_value=0,
                value=2000,
            )

            risk_dollar_medium = st.number_input(
                "Exposure per MEDIUM severity issue ($)",
                min_value=0,
                value=15000,
            )

            risk_dollar_high = st.number_input(
                "Exposure per HIGH severity issue ($)",
                min_value=0,
                value=75000,
            )

    risk_dollar = {
        "low": risk_dollar_low,
        "medium": risk_dollar_medium,
        "high": risk_dollar_high,
    }

    # --- Review cycle time & cost saved ---
    manual_hours = (total_items * minutes_per_item_manual) / 60
    ai_hours = ai_minutes_total / 60
    hours_saved_per_contract = max(manual_hours - ai_hours, 0)
    cost_saved_per_contract = hours_saved_per_contract * hourly_rate
    annual_cost_saved = cost_saved_per_contract * contracts_per_year

    # --- Risk exposure surfaced by flagged/failed findings ---
    risk_exposure_avoided = sum(
        risk_dollar.get(f.severity, 0)
        for f in findings
        if f.status in ("flag", "fail")
    )

    total_impact = cost_saved_per_contract + risk_exposure_avoided

    st.divider()

    st.markdown("#### Estimated Impact — This Contract")

    c1, c2, c3, c4 = st.columns(4)

    c1.metric("Reviewer Hours Saved", f"{hours_saved_per_contract:.1f} hrs")

    c2.metric("Review Cost Saved", f"${cost_saved_per_contract:,.0f}")

    c3.metric("Risk Exposure Flagged", f"${risk_exposure_avoided:,.0f}")

    c4.metric("Total Business Impact", f"${total_impact:,.0f}")

    st.markdown("#### Estimated Impact — Annualized (at current volume)")

    c1, c2 = st.columns(2)

    c1.metric("Annual Review Cost Saved", f"${annual_cost_saved:,.0f}")

    c2.metric("Contracts Covered / Year", int(contracts_per_year))

    st.caption(
        "Risk exposure is the sum of assumed dollar exposure for every flagged "
        "or failed checklist item — i.e. issues the AI review surfaced before "
        "execution. Treat it as an upper bound, not a guaranteed loss avoided."
    )

    # Persist for the final report
    st.session_state.business_impact = {
        "manual_hours": manual_hours,
        "ai_hours": ai_hours,
        "hours_saved_per_contract": hours_saved_per_contract,
        "cost_saved_per_contract": cost_saved_per_contract,
        "annual_cost_saved": annual_cost_saved,
        "risk_exposure_avoided": risk_exposure_avoided,
        "total_impact": total_impact,
        "contracts_per_year": contracts_per_year,
        "assumptions": {
            "minutes_per_item_manual": minutes_per_item_manual,
            "hourly_rate": hourly_rate,
            "ai_minutes_total": ai_minutes_total,
            "contracts_per_year": contracts_per_year,
            "risk_dollar": risk_dollar,
        },
    }

    st.divider()

    c1, c2 = st.columns([1, 1])

    with c1:

        if st.button("⬅ Back to Review", use_container_width=True):

            st.session_state.page = "review"

            st.session_state.wizard_step = 4

            st.rerun()

    with c2:

        if st.button("Generate Final Report ➜", type="primary", use_container_width=True):

            st.session_state.page = "report"

            st.session_state.wizard_step = 6

            st.rerun()

# ------------------------------------------
# Final Report
# ------------------------------------------


def report_page():

    wizard()

    st.title("Step 6")

    st.subheader("Final Report")

    findings = st.session_state.get("review_findings", [])

    decisions = st.session_state.get("decisions", {})

    business_impact = st.session_state.get("business_impact", {})

    contract_name = st.session_state.get("contract_name", "Uploaded Contract")

    risk = {"low": 1, "medium": 3, "high": 5}

    score = sum(risk.get(f.severity, 0) for f in findings if f.status != "pass")

    fail_n = len([f for f in findings if f.status == "fail"])

    flag_n = len([f for f in findings if f.status == "flag"])

    pass_n = len([f for f in findings if f.status == "pass"])

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")

    st.markdown(f"**Contract:** {contract_name}")

    st.caption(f"Generated {generated_at}")

    st.markdown("#### Overall Dashboard")

    c1, c2, c3, c4 = st.columns(4)

    c1.metric("Risk Score", score)

    c2.metric("Failures", fail_n)

    c3.metric("Flags", flag_n)

    c4.metric("Passed", pass_n)

    if business_impact:

        st.markdown("#### Business Impact")

        c1, c2, c3 = st.columns(3)

        c1.metric("Hours Saved", f"{business_impact.get('hours_saved_per_contract', 0):.1f} hrs")

        c2.metric("Cost Saved", f"${business_impact.get('cost_saved_per_contract', 0):,.0f}")

        c3.metric("Risk Exposure Flagged", f"${business_impact.get('risk_exposure_avoided', 0):,.0f}")

    else:

        st.info("Visit the Business Impact step to generate cost/time estimates for this report.")

    st.divider()

    st.markdown("#### Reviewer Decisions & Reasons")

    reviewed = [d for d in decisions.values() if d.get("decision") in ("Accept", "Reject")]

    if not reviewed:

        st.info("No items have been accepted or rejected yet. Go back to the Review step to record decisions.")

    else:

        for d in reviewed:

            icon = "✅" if d["decision"] == "Accept" else "❌"

            with st.expander(f"{icon} {d['decision']} — {d['checklist_item']}"):

                st.write(f"**Category:** {d['category'].replace('_',' ').title()}")

                st.write(f"**Severity:** {d['severity'].title()} • **Clause:** {d['clause_reference']}")

                st.write(f"**Issue:** {d['issue']}")

                st.write(f"**Recommendation:** {d['recommendation']}")

                if d.get("reason"):

                    st.info(f"**Reason:** {d['reason']}")

    st.divider()

    st.markdown("#### Download Report")

    report_data = {
        "contract_name": contract_name,
        "generated_at": generated_at,
        "score": score,
        "fail_n": fail_n,
        "flag_n": flag_n,
        "pass_n": pass_n,
        "findings": findings,
        "decisions": decisions,
        "business_impact": business_impact,
    }

    file_stub = Path(contract_name).stem or "MSA_Review"

    col1, col2 = st.columns(2)

    with col1:

        docx_bytes = build_docx_report(report_data)

        st.download_button(
            "⬇ Download DOCX",
            data=docx_bytes,
            file_name=f"{file_stub}_Review_Report.docx",
            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            use_container_width=True,
        )

    with col2:

        pdf_bytes = build_pdf_report(report_data)

        st.download_button(
            "⬇ Download PDF",
            data=pdf_bytes,
            file_name=f"{file_stub}_Review_Report.pdf",
            mime="application/pdf",
            use_container_width=True,
        )

    # Persist this review to history — guarded so a Streamlit rerun (e.g.
    # clicking a download button) doesn't insert a duplicate row. Resets to
    # None only when a new contract is uploaded (see upload_page()).
    if st.session_state.saved_review_id is None:

        st.session_state.saved_review_id = reviewdb.save_review(
            contract_name=contract_name,
            score=score,
            fail_n=fail_n,
            flag_n=flag_n,
            pass_n=pass_n,
            docx_bytes=docx_bytes,
            pdf_bytes=pdf_bytes,
        )

    st.divider()

    if st.button("⬅ Back to Business Impact"):

        st.session_state.page = "roi"

        st.session_state.wizard_step = 5

        st.rerun()

# ------------------------------------------
# Main
# ------------------------------------------

if not st.session_state.logged_in:

    login_page()

else:

    sidebar()

    if st.session_state.page == "dashboard":

        dashboard()

    elif st.session_state.page == "upload":

        upload_page()

    elif st.session_state.page == "extraction":
        extraction_page()
    elif st.session_state.page == "ai_review":
        ai_review_page()
    elif st.session_state.page == "review":
        review_page()
    elif st.session_state.page == "roi":
        roi_page()
    elif st.session_state.page == "report":
        report_page()