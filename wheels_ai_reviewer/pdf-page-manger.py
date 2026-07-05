import io
import os
import streamlit as st
from pypdf import PdfReader, PdfWriter
from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.ai.documentintelligence.models import AnalyzeDocumentRequest
from azure.core.credentials import AzureKeyCredential
import config

endpoint = config.AZURE_DOC_INTELLIGENCE_ENDPOINT
key = config.AZURE_DOC_INTELLIGENCE_KEY
def extract_markdown_per_page(pdf_path: str) -> list[str]:
    """
    Splits a PDF into individual pages, sends each page to Azure Document
    Intelligence separately, and returns a list of markdown strings
    (one entry per page). Shows live, real (not simulated) progress in the
    Streamlit UI since each page is its own API call.
    """
    # print
    client = DocumentIntelligenceClient(endpoint=endpoint, credential=AzureKeyCredential(key))


    reader = PdfReader(pdf_path)
    total_pages = len(reader.pages)

    progress = st.progress(0, text=f"Starting extraction of {total_pages} pages...")
    status = st.empty()

    page_markdowns = []

    for i in range(total_pages):
        page_num = i + 1
        status.info(f"📄 Extracting page {page_num} of {total_pages}...")

        # Build a single-page PDF in memory
        writer = PdfWriter()
        writer.add_page(reader.pages[i])
        buf = io.BytesIO()
        writer.write(buf)
        buf.seek(0)

        # Run Azure DI on just this page, asking for markdown output
        poller = client.begin_analyze_document(
            "prebuilt-layout",
            AnalyzeDocumentRequest(bytes_source=buf.read()),
            output_content_format="markdown",
        )
        result = poller.result()

        page_markdowns.append(result.content)

        progress.progress(
            page_num / total_pages,
            text=f"{page_num}/{total_pages} pages extracted",
        )

    status.success(f"✅ Extraction complete — {total_pages} pages converted to Markdown")

    return page_markdowns



if __name__ == "__main__":
    # Example usage: run this script directly to test the extraction on a sample PDF
    PDF_PATH = r"D:\programming\WheelsAssignment\Docs\WheelsAssignment.pdf"  # Path to your PDF file
    markdowns = extract_markdown_per_page(PDF_PATH)
    for idx, md in enumerate(markdowns):
        print(f"--- Page {idx + 1} ---")
        print(md)