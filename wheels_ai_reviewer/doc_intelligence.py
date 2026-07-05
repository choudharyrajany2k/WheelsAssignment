# import libraries
import os
from azure.core.credentials import AzureKeyCredential
from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.ai.documentintelligence.models import AnalyzeResult
from azure.ai.documentintelligence.models import AnalyzeDocumentRequest, DocumentContentFormat
import config

# set `<your-endpoint>` and `<your-key>` variables with the values from the Azure portal
endpoint = config.AZURE_DOC_INTELLIGENCE_ENDPOINT
key = config.AZURE_DOC_INTELLIGENCE_KEY

# helper functions

def get_words(page, line):
    result = []
    for word in page.words:
        if _in_span(word, line.spans):
            result.append(word)
    return result


def _in_span(word, spans):
    for span in spans:
        if word.span.offset >= span.offset and (
            word.span.offset + word.span.length
        ) <= (span.offset + span.length):
            return True
    return False


def analyze_layout():
    # sample document
    formUrl = "https://raw.githubusercontent.com/Azure-Samples/cognitive-services-REST-api-samples/master/curl/form-recognizer/sample-layout.pdf"

    document_intelligence_client = DocumentIntelligenceClient(
        endpoint=endpoint, credential=AzureKeyCredential(key)
    )

    poller = document_intelligence_client.begin_analyze_document(
        "prebuilt-layout", AnalyzeDocumentRequest(url_source=formUrl
    ))

    result: AnalyzeResult = poller.result()

    if result.styles and any([style.is_handwritten for style in result.styles]):
        print("Document contains handwritten content")
    else:
        print("Document does not contain handwritten content")

    for page in result.pages:
        print(f"----Analyzing layout from page #{page.page_number}----")
        print(
            f"Page has width: {page.width} and height: {page.height}, measured with unit: {page.unit}"
        )

        if page.lines:
            for line_idx, line in enumerate(page.lines):
                words = get_words(page, line)
                print(
                    f"...Line # {line_idx} has word count {len(words)} and text '{line.content}' "
                    f"within bounding polygon '{line.polygon}'"
                )

                for word in words:
                    print(
                        f"......Word '{word.content}' has a confidence of {word.confidence}"
                    )

        if page.selection_marks:
            for selection_mark in page.selection_marks:
                print(
                    f"Selection mark is '{selection_mark.state}' within bounding polygon "
                    f"'{selection_mark.polygon}' and has a confidence of {selection_mark.confidence}"
                )

    if result.tables:
        for table_idx, table in enumerate(result.tables):
            print(
                f"Table # {table_idx} has {table.row_count} rows and "
                f"{table.column_count} columns"
            )
            if table.bounding_regions:
                for region in table.bounding_regions:
                    print(
                        f"Table # {table_idx} location on page: {region.page_number} is {region.polygon}"
                    )
            for cell in table.cells:
                print(
                    f"...Cell[{cell.row_index}][{cell.column_index}] has text '{cell.content}'"
                )
                if cell.bounding_regions:
                    for region in cell.bounding_regions:
                        print(
                            f"...content on page {region.page_number} is within bounding polygon '{region.polygon}'"
                        )

    print("----------------------------------------")

def convert_pdf_to_markdown():
    PDF_PATH = r"D:\programming\WheelsAssignment\Docs\WheelsAssignment.pdf"  # Path to your PDF file
    print(f"Initializing client and opening {PDF_PATH}...")
    client = DocumentIntelligenceClient(endpoint=endpoint, credential=AzureKeyCredential(key))
    
    # with open(PDF_PATH, "rb") as f:
    #     poller = client.begin_analyze_document(
    #         model_id="prebuilt-layout", 
    #         document=f,
    #         output_content_format="markdown",  # Instructs Azure to output Markdown syntax
    #         body=f
    #     )
    with open(PDF_PATH, "rb") as f:
        poller = client.begin_analyze_document(
        model_id="prebuilt-layout",
        body=f,
        output_content_format=DocumentContentFormat.MARKDOWN,  # key part
    )
        
    print("Processing document on Azure (this may take a moment for 16 pages)...")
    result = poller.result()
    
    # Save the extracted markdown text
    output_file = "output.md"
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(result.content)
        
    print(f"Success! Markdown saved to {output_file}")


if __name__ == "__main__":
    convert_pdf_to_markdown()