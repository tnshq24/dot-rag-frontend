import hashlib
import fitz
import re
from collections import defaultdict
from io import BytesIO

def authenticate_user(email, password):
    """Simple authentication - in production, use proper auth"""
    # For now, hardcoded credentials as requested
    if email == "admin@xyz.com" and password == "admin":
        return True
    elif email == "user1@xyz.com" and password == "user1":
        return True
    return False


def generate_user_id(email):
    """Generate a consistent user ID from email"""
    return hashlib.md5(email.encode()).hexdigest()


def higlight_pdf_content(full_content, all_pages, doc, rag_pipeline):
    from sklearn.metrics.pairwise import cosine_similarity
    from sklearn.feature_extraction.text import TfidfVectorizer
    found = False
    for idx, content in enumerate(full_content):
        target_page = int(all_pages[idx]) - 1
        try:
            for page_num, page in enumerate(doc):
                if target_page is not None and page_num != target_page:
                    continue

                # Get page text
                page_text = page.get_text()
                if not page_text.strip():
                    continue

                # Try to find and highlight the content
                try:
                    chunks = [content] + rag_pipeline.chunk_text(text=page_text)
                    vectorizer = TfidfVectorizer()
                    vect_text = vectorizer.fit_transform(chunks)
                    similarities = cosine_similarity(vect_text[0:1], vect_text[1:]).flatten()
                    best_match_index = similarities.argmax() + 1
                    similar_text = chunks[best_match_index]

                    # Search for the text in the page
                    text_instances = page.search_for(similar_text)
                    if text_instances:
                        for inst in text_instances:
                            highlight = page.add_highlight_annot(inst)
                            highlight.update()
                        found = True
                        #print(f"Successfully highlighted text on page {page_num + 1}")
                        break
                    else:
                        print(f"No text instances found on page {page_num + 1}")
                except Exception as e:
                    print(f"Error highlighting on page {page_num + 1}: {str(e)}")
                    # Continue to next page if highlighting fails
                    continue
        except Exception as e:
            print(f"Error in highlighting process: {str(e)}")
            # Continue without highlighting
    return doc, found

def highlight_scanned_pdf_content(full_content, all_pages, doc, page_content):
    for idx, content in enumerate(full_content):
        target_page = int(all_pages[idx]) - 1
        try:
            for page_num, page in enumerate(doc):
                if target_page is not None and page_num != target_page:
                    continue

                page_text = page_content.pages[target_page].lines
                for line in page_text:
                    if line.content in content:
                        x_coords = [line.polygon[i] * 72 for i in range(0, 8, 2)]
                        y_coords = [line.polygon[i] * 72 for i in range(1, 8, 2)]
                        x0, x1 = min(x_coords), max(x_coords)
                        y0, y1 = min(y_coords), max(y_coords)

                        rect = fitz.Rect(x0, y0, x1, y1)
                        page.add_highlight_annot(rect)
        except Exception as e:
            print(f"Error highlighting on page {target_page + 1}: {str(e)}")
    return doc


def get_highlighted_pdf_content(rag_pipeline, source, try_highlight=True):
    all_content = source["content"]
    all_pages = source["page_number"]
    # Download the PDF content from blob storage
    pdf_content = rag_pipeline.get_pdf_content_from_blob(blob_name=source["filename"])
    # Create a BytesIO object to read the PDF content
    doc = fitz.open(stream=pdf_content, filetype="pdf")
    found = False
    if try_highlight:

        if "pages_content" in source.keys():
            doc = highlight_scanned_pdf_content(
                full_content=all_content,
                all_pages=all_pages,
                doc=doc,
                page_content=source["pages_content"]
            )
            found = True
        else:
            doc, found = higlight_pdf_content(
                full_content=all_content,
                all_pages=all_pages,
                doc=doc,
                rag_pipeline=rag_pipeline
            )
    output_pdf_io = BytesIO()
    doc.save(output_pdf_io)
    doc.close()
    output_pdf_io.seek(0)
    return output_pdf_io, found

def extract_refs_dict(text: str) -> dict[str, list[int]]:
    """
    Return {filename: [pages, …], …} from
    • in-line refs like “… (foo.pdf, Page 3)”
    • bulleted refs like “- foo.pdf, Pages 3 and 20.”
    """
    pattern = re.compile(
        r"(?:\(\s*|^\s*-\s*)"  # “( …”  or  “- …” at line start
        r"(?P<file>[^,()]+?\.pdf)"  # filename ending in .pdf
        r"\s*,\s*Page(?:s)?\s*"  # “, Page ” / “, Pages ”
        r"(?P<nums>[^)\n\.]+)",  # everything up to “)” / eol / period
        flags=re.I | re.M,
    )

    refs = defaultdict(list)

    for m in pattern.finditer(text):
        filename = m.group("file").strip()
        pages = [int(n) for n in re.findall(r"\d+", m.group("nums"))]
        refs[filename].extend(pages)

    # deduplicate & sort each page list
    return {f: sorted(set(ps)) for f, ps in refs.items()}

def extract_refs_dict_v2(text: str) -> dict[str, list[int]]:
    result = defaultdict(list)

    # Regex: match lines with .pdf filename and flexible Page/Pages declaration
    pattern = re.compile(
        r'[\s\-•]*'  # optional leading dash, bullet, or whitespace
        r'(?P<filename>[\w\s\-()&_]+\.pdf)\s*,?\s*'  # PDF filename
        r'Pages?\s*:?\s*'  # "Page" or "Pages", with optional colon
        r'(?P<pages>[\d\s, and]+)',  # page numbers
        re.IGNORECASE
    )

    for match in pattern.finditer(text):
        filename = match.group('filename').strip().lstrip('-• ').strip()
        pages_raw = match.group('pages')
        # Normalize: "and" → "," and split
        pages_clean = re.sub(r'\band\b', ',', pages_raw, flags=re.IGNORECASE)
        pages = [int(p.strip()) for p in pages_clean.split(',') if p.strip().isdigit()]
        result[filename].extend(pages)

    return dict(result)

def extract_pdf_references(text: str) -> dict[str, list[int]]:
    def _expand_pages(pages_str: str):
        # Normalize separators
        s = re.sub(r'\band\b', ',', pages_str, flags=re.IGNORECASE)
        s = s.replace('–', '-').replace('—', '-')
        tokens = [t.strip() for t in s.split(',') if t.strip()]

        out = []
        for t in tokens:
            m = _PAGE_TOKEN.fullmatch(t)
            if not m:
                continue
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else start
            if end >= start:
                out.extend(range(start, end + 1))
        return out

    # Matches a single page "7" or a range "7-10" (also supports en/em dashes)
    _PAGE_TOKEN = re.compile(r'(\d+)\s*(?:[-–—]\s*(\d+))?$')

    result = defaultdict(list)

    # Anchor per line; don't let pages bleed to the next line
    # line_pattern = re.compile(
    #     r'^\s*(?:[-•\u2022]|\d+[.)])?\s*'          # optional bullet or "1." / "1)"
    #     r'(?P<filename>[\w\s\-()&_]+\.pdf)\s*,?\s*'
    #     r'(?:Pages?|Pg|PP|Pgs?)\s*:?\s*'
    #     r'(?P<pages>[^\r\n]+?)\s*$',               # capture up to end of line (no newline)
    #     re.IGNORECASE | re.MULTILINE
    # )

    line_pattern = re.compile(
        r'[\s\-•]*'  # optional leading dash, bullet, or whitespace
        r'(?P<filename>[\w\s\-()&_]+\.pdf)\s*,?\s*'  # PDF filename
        r'Pages?\s*:?\s*'  # "Page" or "Pages", with optional colon
        r'(?P<pages>[\d\s, and]+)',  # page numbers
        re.IGNORECASE
    )


    for m in line_pattern.finditer(text):
        filename = m.group('filename').strip()
        pages = _expand_pages(m.group('pages'))
        if pages:
            # dedupe & sort
            result[filename] = sorted(set(pages))
    return dict(result)


def get_relevant_sources(result, response):
    relevant_sources = {}

    for filename in result:
        cleaned_source_filename = filename.split("/")[-1].strip()
        cleaned_source_filename = cleaned_source_filename.replace("--\n\n\nReferences:\n- ", "").strip()
        allowed_pages = result[filename]
        for doc in response["source_documents"]:
            cleaned_retrieved_filename = doc["filename"].split("/")[-1].strip()
            if isinstance(doc["page_number"], list):
                page_number = int(doc["page_number"][0])
            else:
                page_number = int(doc["page_number"])

            if (
                    (cleaned_retrieved_filename.lower() == cleaned_source_filename.lower()) and (page_number in allowed_pages)
            ):
                if cleaned_retrieved_filename in relevant_sources:
                    relevant_sources[cleaned_retrieved_filename]["content"].append(doc["content"])
                    relevant_sources[cleaned_retrieved_filename]["page_number"].append(doc["page_number"])
                    print(relevant_sources[cleaned_retrieved_filename]["page_number"])
                else:
                    relevant_sources[cleaned_retrieved_filename] = doc
                    relevant_sources[cleaned_retrieved_filename]["content"] = [doc["content"]]
                    relevant_sources[cleaned_retrieved_filename]["page_number"] = [doc["page_number"]]
                # relevant_sources.append(doc)
                # seen_files.add(cleaned_source_filename)
    relevant_sources = [relevant_sources[file_name] for file_name in relevant_sources]

    # pdf_to_consider = [ans for ans in response["answer"].split() if ans in file_names]
    # if len(pdf_to_consider)!=len(to_consider):
    #     pdf_to_consider = {file for file in file_names for s_file in to_consider if SequenceMatcher(None, file, s_file).ratio() > 0.9}

    # print("PDF to Consider : ", pdf_to_consider)
    # reference_sources = []
    # added_file = []
    # for file in response["source_documents"]:
    #     if  file["filename"] in pdf_to_consider and file["filename"] not in added_file:
    #         reference_sources.append(file)
    #         added_file.append(file["filename"])

    # print(reference_sources)


    return relevant_sources