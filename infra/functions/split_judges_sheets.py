import re
import os
import sys
from pypdf import PdfReader, PdfWriter

def slugify(text):
    # Simple slugify to normalize filenames
    text = text.lower()
    replacements = {
        'ä': 'a', 'ö': 'o', 'å': 'a',
        'ü': 'u', 'é': 'e', 'è': 'e',
        ' ': '_'
    }
    for k, v in replacements.items():
        text = text.replace(k, v)
    # Remove any remaining non-alphanumeric characters (except underscore)
    text = re.sub(r'[^a-z0-9_]', '', text)
    return text

def parse_judge_line(line):
    # Split by 2 or more spaces to separate the Role column from the Name column
    parts = re.split(r'\s{2,}', line.strip())
    
    if len(parts) < 2:
        return None, None
        
    role_part = parts[0].strip()
    name_part = parts[1].strip()
    
    role = None
    # Identify role based on prefix
    if role_part.startswith("Referee"):
        role = "referee"
    elif role_part.startswith("Judge"):
        role = "judge"
    elif role_part.startswith("Technical Controller"):
        role = "technical_controller"
    elif role_part.startswith("Technical Specialist"):
        role = "technical_specialist"
    elif role_part.startswith("Data Operator"):
        role = "data_operator"
    elif role_part.startswith("Replay Operator"):
        role = "replay_operator"
        
    if not role:
        return None, None
        
    # Clean name: remove titles like Ms., Mr., Dr.
    # Also remove any leading/trailing whitespace
    clean_name_part = re.sub(r'^(Ms\.|Mr\.|Dr\.)\s*', '', name_part).strip()
    name = slugify(clean_name_part)
    
    return role, name

def extract_starting_number(page_text):
    """
    Extract the starting number and competitor name from a JudgesSheetAll page.
    
    The page layout (after layout extraction) has header lines (competition name,
    category/segment, referee info) followed by the competitor line whose first
    column is a starting number (digit) or 'WD' for withdrawn.
    
    Instead of relying on a fixed line index, we scan all lines for the first
    one where the first column is a number or 'WD'.
    
    Returns (starting_number, competitor_name) or (None, None) if not found.
    """
    lines = page_text.split('\n')
    
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        
        # Split by 2+ spaces to get columns
        parts = re.split(r'\s{2,}', stripped)
        
        if len(parts) < 2:
            continue
        
        first_col = parts[0].strip()
        
        # Check if first column is a starting number (digit) or 'WD'
        if first_col.isdigit() or first_col.upper() == 'WD':
            competitor_name = parts[1].strip()
            return first_col.upper(), competitor_name
    
    return None, None


# A header line marking a withdrawn skater: 'WD' followed by the name.
# The negative lookahead skips the 'WD  Withdrawn' legend that start lists
# and similar summary pages carry.
WITHDRAWN_HEADER_RE = re.compile(r'^WD\s+(?!Withdrawn\b)\S')

# How many non-empty lines from the top of a page count as the FSM header block.
WITHDRAWN_HEADER_LINES = 8


def _withdrawn_header_line(page_text):
    """
    Return the header line marking this page as a withdrawn skater's, or None.

    Only the first few non-empty lines (the FSM header block) are scanned: the
    element rows further down never look like 'WD <name>', but a skater name
    can appear lower on some tables (e.g. a per-segment list), and those pages
    must not be treated as a single withdrawn skater's page.
    """
    seen = 0
    for line in page_text.split('\n'):
        stripped = line.strip()
        if not stripped:
            continue
        if WITHDRAWN_HEADER_RE.match(stripped):
            return stripped
        seen += 1
        if seen >= WITHDRAWN_HEADER_LINES:
            break
    return None


def _is_withdrawn_legend(name):
    """True for the 'WD  Withdrawn' legend line's second column."""
    return bool(name) and bool(re.match(r'^Withdrawn\b', name.strip()))


def is_withdrawn_page(page_text):
    """
    True if this page belongs to a single withdrawn skater.

    FSM prints 'WD' where the starting number would be, in two column styles:
    the JudgesSheetAll pages separate the columns with 2+ spaces
    ('WD    Ines SALMINEN    HTA'), while the Technical Controller / Specialist
    sheets use a single space ('WD Ines SALMINEN     HTA') — which is why
    extract_starting_number (2+ space columns) alone is not enough. Both styles
    are covered: the header-block scan handles the single-space layout and
    extract_starting_number handles the wide-column one.
    """
    if _withdrawn_header_line(page_text):
        return True
    starting_num, competitor_name = extract_starting_number(page_text)
    # 'WD  Withdrawn' is the legend of a start list / planned program content
    # page, not a withdrawn skater's own page.
    return starting_num == 'WD' and not _is_withdrawn_legend(competitor_name)


def withdrawn_name_from_page(page_text):
    """
    Competitor name from a withdrawn skater's page, or None.

    Used for the cover page's "Withdrawn:" list.
    """
    starting_num, competitor_name = extract_starting_number(page_text)
    if starting_num == 'WD' and competitor_name and not _is_withdrawn_legend(competitor_name):
        return competitor_name

    line = _withdrawn_header_line(page_text)
    if not line:
        return None

    rest = re.sub(r'^WD\s+', '', line).strip()
    if not rest:
        return None
    # Drop the trailing club column (separated by 2+ spaces)
    return re.split(r'\s{2,}', rest)[0].strip() or None


def filter_withdrawn_pages(input_pdf, output_pdf):
    """
    Copy input_pdf to output_pdf without the pages of withdrawn skaters.

    Used for the per-skater Technical Controller / Technical Specialist sheets,
    which are merged into the packets whole. Pages whose text cannot be read
    are kept. Returns the number of pages removed (0 if input_pdf is missing,
    in which case nothing is written).
    """
    if not os.path.exists(input_pdf):
        return 0

    reader = PdfReader(input_pdf)
    writer = PdfWriter()
    removed = 0

    for i, page in enumerate(reader.pages):
        try:
            text = page.extract_text(extraction_mode="layout")
        except Exception as e:
            print(f"  Warning: Could not read page {i+1} of {os.path.basename(input_pdf)}: {e}")
            text = None

        if text and is_withdrawn_page(text):
            removed += 1
            continue

        writer.add_page(page)

    with open(output_pdf, "wb") as f:
        writer.write(f)

    return removed


def split_pdf(pdf_path):
    """
    Split a JudgesSheetAll PDF into per-judge/referee PDFs.
    Pages for withdrawn competitors (starting number 'WD') are excluded.
    
    Returns a list of withdrawn competitor names (empty list if none).
    """
    if not os.path.exists(pdf_path):
        print(f"Error: File {pdf_path} not found.")
        return []

    print(f"Processing {pdf_path}...")
    reader = PdfReader(pdf_path)
    base_name = os.path.splitext(os.path.basename(pdf_path))[0]
    output_dir = os.path.dirname(pdf_path)
    
    current_judge_key = None # Tuple of (role, name)
    current_pages = []
    withdrawn_competitors = []  # Track withdrawn competitor names
    
    for i, page in enumerate(reader.pages):
        text = page.extract_text(extraction_mode="layout")
        lines = text.split('\n')
        
        # Check if this page is for a withdrawn competitor
        starting_num, competitor_name = extract_starting_number(text)
        if starting_num == 'WD' or is_withdrawn_page(text):
            name = withdrawn_name_from_page(text) or competitor_name
            if name and name not in withdrawn_competitors:
                withdrawn_competitors.append(name)
            print(f"  Skipping page {i+1}: Withdrawn competitor '{name}'")
            continue  # Skip this page entirely
        
        found_judge_on_page = False
        
        # Scan lines to find the judge info
        for line in lines:
            role, name = parse_judge_line(line)
            if role and name:
                key = (role, name)
                
                # If we found a judge info, check if it's different from the current one
                if key != current_judge_key:
                    # If we have accumulated pages for a previous judge, save them
                    if current_pages:
                        save_pages(current_pages, current_judge_key, base_name, output_dir)
                        current_pages = []
                    
                    current_judge_key = key
                    print(f"Found new judge section: {role} {name} starting at page {i+1}")
                
                found_judge_on_page = True
                break # Stop scanning lines on this page once judge is found
        
        if found_judge_on_page:
            current_pages.append(page)
        else:
            # If no judge info found on this page, assume it belongs to the current judge
            # (e.g. continuation page, though unlikely for judge sheets which are usually 1 page per skater)
            if current_judge_key:
                current_pages.append(page)
            else:
                print(f"Warning: Page {i+1} has no judge info and no current judge context. Skipping.")

    # Save the last batch of pages
    if current_pages:
        save_pages(current_pages, current_judge_key, base_name, output_dir)
    else:
        print("No pages were saved.")
    
    if withdrawn_competitors:
        print(f"  Withdrawn competitors: {', '.join(withdrawn_competitors)}")
    
    return withdrawn_competitors

def save_pages(pages, judge_key, base_name, output_dir):
    if not judge_key:
        # Should not happen if logic is correct
        return
        
    role, name = judge_key
    filename = f"{base_name}_{role}_{name}.pdf"
    filepath = os.path.join(output_dir, filename)
    
    writer = PdfWriter()
    for page in pages:
        writer.add_page(page)
        
    with open(filepath, "wb") as f:
        writer.write(f)
    print(f"Saved {filepath} ({len(pages)} pages)")
