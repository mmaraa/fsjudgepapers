import os
import sys
import re
import zipfile
from datetime import datetime

# Import functions from existing scripts
try:
    from .split_judges_sheets import split_pdf, filter_withdrawn_pages
    from .create_cover_pages import create_cover_pdf, create_segment_cover_pdf, create_start_list_with_strikethrough, extract_title_from_pdf, extract_segment_name_from_pdf
    from .combine_judging_papers import get_date_from_start_list, get_first_start_time_from_start_list, get_panel_info, merge_pdfs, slugify
    from .categories import load_categories, match_category, parse_filename_generic
    from .competition_schedule import parse_competition_schedule, get_schedule_start_time
except ImportError:
    # Fallback for local testing if not running as package
    from split_judges_sheets import split_pdf, filter_withdrawn_pages
    from create_cover_pages import create_cover_pdf, create_segment_cover_pdf, create_start_list_with_strikethrough, extract_title_from_pdf, extract_segment_name_from_pdf
    from combine_judging_papers import get_date_from_start_list, get_first_start_time_from_start_list, get_panel_info, merge_pdfs, slugify
    from categories import load_categories, match_category, parse_filename_generic
    from competition_schedule import parse_competition_schedule, get_schedule_start_time


def _get_categories_for_processor():
    """
    Load categories from Azure Table for use in the processor.
    Returns cached categories list, or empty list if table is unavailable.
    """
    try:
        from azure.data.tables import TableClient
        from azure.identity import DefaultAzureCredential
        account_name = os.environ.get("AzureWebJobsStorage__accountName")
        if account_name:
            credential = DefaultAzureCredential()
            endpoint = f"https://{account_name}.table.core.windows.net"
            table_client = TableClient(endpoint=endpoint, table_name="categories", credential=credential)
            return load_categories(table_client)
        
        connection_string = os.environ.get("AzureWebJobsStorage")
        if connection_string:
            table_client = TableClient.from_connection_string(conn_str=connection_string, table_name="categories")
            return load_categories(table_client)
    except Exception as e:
        print(f"  Warning: Could not load categories table: {e}")
    return []


def parse_prefix(prefix, categories=None, language="fi"):
    """
    Parses a filename prefix (without suffix) to extract category display name
    and segment name using the categories table.
    Returns (category_name, segment_name) or (None, None) if not matched.
    
    Args:
        language: 'fi' for Finnish names (default), 'en' for English names.
    """
    if categories is None:
        categories = _get_categories_for_processor()
    
    if not categories:
        return None, None

    matched = match_category(prefix, categories)
    if not matched:
        return None, None
    
    abbrev_len = len(matched["abbreviation"])
    remainder = prefix[abbrev_len:]
    
    # Detect split/group number
    stripped = remainder.lstrip("-")
    split_number = None
    if language == "en":
        display_name = matched["displayName"]
    else:
        display_name = matched.get("displayNameFi", matched["displayName"])
    if stripped and len(stripped) >= 2 and stripped[:2].isdigit():
        split_number = int(stripped[:2])
        display_name = f"{display_name} #{split_number}"

    # Segment detection — extract full segment identifier from remainder
    seg_stripped = remainder.strip("-")
    if split_number is not None and seg_stripped and len(seg_stripped) >= 2 and seg_stripped[:2].isdigit():
        seg_stripped = seg_stripped[2:].lstrip("-")

    segment = None
    if "CalculationSetupVerificationforReferee" in prefix:
        segment = "Category General"
    elif seg_stripped:
        segment = seg_stripped

    return display_name, segment


def get_abbreviation_length(prefix, categories=None):
    """
    Returns the length of the matched abbreviation for a given prefix,
    used for CalculationSetup file matching. Returns None if no match.
    """
    if categories is None:
        categories = _get_categories_for_processor()
    
    matched = match_category(prefix, categories)
    if matched:
        return len(matched["abbreviation"])
    return None

def process_judging_papers(source_dir, output_dir, options=None):
    if options is None:
        options = {}
    
    # Language setting: 'fi' (Finnish, default) or 'en' (English)
    language = options.get('language', 'fi')
    
    # Create output directory if it doesn't exist
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
        print(f"Created output directory: {output_dir}")
        
    print(f"Processing files from: {source_dir}")
    print(f"Saving results to: {output_dir}")
    
    # Pre-load categories once for the entire processing run
    categories = _get_categories_for_processor()
    
    # ---------------------------------------------------------
    # 0. Parse CompetitionSchedule (for start times & segment names)
    # ---------------------------------------------------------
    print("\n[0] Parsing CompetitionSchedule...")
    schedule_entries = []
    prefix_start_times = {}   # prefix -> "HH:MM" start time
    prefix_segment_names = {} # prefix -> actual segment name from JudgesSheetAll
    prefix_category_names = {} # prefix -> category display name
    prefix_withdrawn = {}     # prefix -> list of withdrawn competitor names

    for root, dirs, files in os.walk(source_dir):
        for file in files:
            if file.endswith("_CompetitionSchedule.pdf"):
                schedule_path = os.path.join(root, file)
                print(f"  Found CompetitionSchedule: {file}")
                schedule_entries = parse_competition_schedule(schedule_path, categories)
                print(f"  Parsed {len(schedule_entries)} schedule entries")
                break
        if schedule_entries:
            break

    # ---------------------------------------------------------
    # 0.5 Extract segment names from JudgesSheetAll (line 2)
    # ---------------------------------------------------------
    print("\n[0.5] Extracting segment names from JudgesSheetAll...")
    for root, dirs, files in os.walk(source_dir):
        for file in files:
            if file.endswith("_JudgesSheetAll.pdf") and "_judge_" not in file and "_referee_" not in file:
                prefix = file.replace("_JudgesSheetAll.pdf", "")
                js_path = os.path.join(root, file)

                # Extract the full second line (segment name line)
                full_seg_line = extract_segment_name_from_pdf(js_path)
                if full_seg_line:
                    # Strip category display name from the beginning to get just the segment name
                    cat_name, _ = parse_prefix(prefix, categories, language=language)
                    seg_name = full_seg_line
                    if cat_name:
                        # Try case-insensitive prefix strip
                        if full_seg_line.upper().startswith(cat_name.upper()):
                            seg_name = full_seg_line[len(cat_name):].strip()
                        # If not a direct match, try stripping with normalized comparison
                        elif cat_name.upper().replace(',', '').replace('-', ' ') in full_seg_line.upper().replace(',', '').replace('-', ' '):
                            # Find where category name ends in the line
                            # Simple approach: find the longest prefix match
                            upper_line = full_seg_line.upper()
                            upper_cat = cat_name.upper()
                            # Walk both strings accounting for punctuation differences
                            best_pos = 0
                            cat_idx = 0
                            for i, ch in enumerate(upper_line):
                                if cat_idx >= len(upper_cat):
                                    best_pos = i
                                    break
                                if ch == upper_cat[cat_idx]:
                                    cat_idx += 1
                                elif ch in ' ,-./()' and (cat_idx == 0 or upper_cat[cat_idx-1] in ' ,-./()' or ch != upper_cat[cat_idx]):
                                    continue
                            if best_pos > 0:
                                seg_name = full_seg_line[best_pos:].strip()

                    if seg_name:
                        prefix_segment_names[prefix] = seg_name
                        if cat_name:
                            prefix_category_names[prefix] = cat_name
                        print(f"  {prefix} -> segment: {seg_name}")

                        # Look up start time from schedule
                        if schedule_entries:
                            start_time = get_schedule_start_time(
                                schedule_entries, cat_name or "", seg_name
                            )
                            if start_time:
                                prefix_start_times[prefix] = start_time
                                print(f"  {prefix} -> start time: {start_time}")

    # ---------------------------------------------------------
    # 1. Split Judges Sheets
    # ---------------------------------------------------------
    print("\n[1/5] Splitting Judges Sheets...")
    for root, dirs, files in os.walk(source_dir):
        for file in files:
            # Look for the main JudgesSheetAll files, excluding already split ones
            if file.endswith("_JudgesSheetAll.pdf") and "_judge_" not in file and "_referee_" not in file:
                full_path = os.path.join(root, file)
                prefix = file.replace("_JudgesSheetAll.pdf", "")
                # We call the split function which generates files in the same folder
                withdrawn = split_pdf(full_path)
                if withdrawn:
                    prefix_withdrawn[prefix] = withdrawn

    # ---------------------------------------------------------
    # 2. Gather Data and Generate Cover Pages
    # ---------------------------------------------------------
    print("\n[2/5] Generating Cover Pages...")
    
    # Data structure to hold tasks for the final merge
    person_tasks = {}
    
    for root, dirs, files in os.walk(source_dir):
        for file in files:
            if file.endswith("_ISUPanelofJudgesandTechnicalPanel.pdf"):
                prefix = file.replace("_ISUPanelofJudgesandTechnicalPanel.pdf", "")
                
                # Construct paths for related files
                start_list_path = os.path.join(root, f"{prefix}_StartListwithTimes.pdf")
                judges_sheet_path = os.path.join(root, f"{prefix}_JudgesSheetAll.pdf")
                panel_path = os.path.join(root, file)
                
                # Validation
                if not os.path.exists(start_list_path):
                    print(f"  Skipping {prefix}: Missing StartList")
                    continue
                
                # Extract Date
                date_str = get_date_from_start_list(start_list_path)
                if not date_str:
                    print(f"  Skipping {prefix}: Could not extract date")
                    continue

                # Extract first start time for segment ordering
                first_time = get_first_start_time_from_start_list(start_list_path)
                if first_time:
                    prefix_start_times[prefix] = first_time
                    print(f"  {prefix} -> start time (from StartList): {first_time}")
                    
                # Extract Title
                title = extract_title_from_pdf(judges_sheet_path)
                
                if not title:
                    cat_name, _ = parse_prefix(prefix, categories, language=language)
                    if cat_name:
                        title = cat_name
                    else:
                        title = "Competition" # Fallback
                    
                # Extract Panel Info (Roles and Names)
                panel = get_panel_info(panel_path)
                
                # Process each person in the panel
                for role, name in panel:
                    slug = slugify(name)
                    
                    if date_str not in person_tasks:
                        person_tasks[date_str] = {}
                    
                    if slug not in person_tasks[date_str]:
                        # Initialize person entry for this date
                        person_tasks[date_str][slug] = {
                            'name': name,
                            'title': title,
                            'tasks': []
                        }
                        
                        # Generate Cover Page
                        # We save it in the source directory (or root of source) to keep it simple
                        cover_filename = f"coverpage_{date_str}_{slug}.pdf"
                        cover_path = os.path.join(root, cover_filename)
                        
                        try:
                            dt = datetime.strptime(date_str, "%Y%m%d")
                            create_cover_pdf(cover_path, title, dt, name)
                        except ValueError:
                            print(f"  Error parsing date {date_str} for cover page")
                        
                    # Add this segment task to the person
                    person_tasks[date_str][slug]['tasks'].append((prefix, role))

    # ---------------------------------------------------------
    # 2.5 Filter Conflicts (Referee overrides Judge)
    # ---------------------------------------------------------
    for date_str, persons in person_tasks.items():
        for slug, data in persons.items():
            tasks = data['tasks']
            prefix_roles = {}
            for prefix, role in tasks:
                if prefix not in prefix_roles:
                    prefix_roles[prefix] = set()
                prefix_roles[prefix].add(role)
            
            cleaned_tasks = []
            for prefix, roles in prefix_roles.items():
                if 'referee' in roles and 'judge' in roles:
                    roles.remove('judge')
                
                for role in roles:
                    cleaned_tasks.append((prefix, role))
            
            data['tasks'] = cleaned_tasks

    # ---------------------------------------------------------
    # 3. Combine Papers
    # ---------------------------------------------------------
    print("\n[3/5] Combining Papers...")
    
    count = 0
    total_pages = 0  # pages across per-person packets (for usage statistics)
    withdrawn_pages_removed = 0  # pages dropped from the per-skater official sheets

    def sheet_without_withdrawn(base_path, suffix):
        """
        Path to a per-skater official sheet (Technical Controller / Specialist)
        with the withdrawn skaters' pages removed. The filtered copy is written
        once per segment and reused by every official on it (the person loop
        runs once per official per segment). Returns the original path when the
        sheet is missing, so merge_pdfs still warns about it.
        """
        nonlocal withdrawn_pages_removed
        src = f"{base_path}_{suffix}.pdf"
        if not os.path.exists(src):
            return src

        dst = f"{base_path}_{suffix}_nowd.pdf"
        if not os.path.exists(dst):
            removed = filter_withdrawn_pages(src, dst)
            if removed:
                print(f"  Removed {removed} withdrawn page(s) from {os.path.basename(src)}")
                withdrawn_pages_removed += removed
        return dst

    for date_str, persons in person_tasks.items():
        for slug, data in persons.items():
            tasks = data['tasks']
            # Sort tasks by start time from CompetitionSchedule (earliest first).
            # Falls back to prefix alphabetical order if no schedule data available.
            tasks.sort(key=lambda x: prefix_start_times.get(x[0], "99:99"))
            
            file_list = []
            
            # 1. Add Cover Page
            cover_filename = f"coverpage_{date_str}_{slug}.pdf"
            cover_path = os.path.join(source_dir, cover_filename) 
            
            if os.path.exists(cover_path):
                file_list.append(cover_path)
            else:
                # Try searching recursively if not found in root
                found = False
                for r, d, f in os.walk(source_dir):
                    if cover_filename in f:
                        file_list.append(os.path.join(r, cover_filename))
                        found = True
                        break
                if not found:
                    print(f"  Warning: Cover page not found for {slug} on {date_str}")

            # 2. Add Segment Files
            for prefix, role in tasks:
                # We need to find where this prefix lives. 
                segment_dir = source_dir
                for r, d, f in os.walk(source_dir):
                    if f"{prefix}_StartListwithTimes.pdf" in f:
                        segment_dir = r
                        break
                
                base_path = os.path.join(segment_dir, prefix)
                start_list_path = f"{base_path}_StartListwithTimes.pdf"
                judges_sheet_path = f"{base_path}_JudgesSheetAll.pdf"

                # Check options for this segment
                # If specific segment prefix is in options, use that.
                # Else check global 'includeSegmentCover' (default false)
                # Structure of options: { 'segmentCovers': { 'PREFIX': True/False }, 'globalSegmentCover': True/False }
                
                use_segment_cover = options.get('globalSegmentCover', False)
                if 'segmentCovers' in options and prefix in options['segmentCovers']:
                    use_segment_cover = options['segmentCovers'][prefix]
                
                if use_segment_cover:
                    # Generate Segment Cover Page
                    segment_cover_filename = f"segmentcover_{prefix}.pdf"
                    segment_cover_path = os.path.join(segment_dir, segment_cover_filename)
                    
                    # Get withdrawn competitors for this segment
                    withdrawn_for_segment = prefix_withdrawn.get(prefix, [])
                    
                    if not os.path.exists(segment_cover_path):
                        # Needs Info: Competition Name, Date, Segment Name
                         try:
                            # 1. Date (Already parsed into date_str YYYYMMDD in person_tasks, assuming consistent)
                            # But we need datetime object.
                            dt_obj = datetime.strptime(date_str, "%Y%m%d")
                            
                            # 2. Competition Name (from data['title'])
                            comp_title = data['title']
                            
                            # 3. Segment Name — prefer pre-extracted name, fall back
                            seg_name = prefix_segment_names.get(prefix)
                            if not seg_name:
                                seg_name = extract_segment_name_from_pdf(judges_sheet_path)
                            if not seg_name:
                                _, parsed_seg = parse_prefix(prefix, categories, language=language)
                                if parsed_seg:
                                    seg_name = parsed_seg
                                else:
                                    seg_name = "Segment"
                            
                            # 4. Category Name for the cover page
                            cat_name_cover = prefix_category_names.get(prefix)
                            if not cat_name_cover:
                                cat_name_cover, _ = parse_prefix(prefix, categories, language=language)
                                
                            create_segment_cover_pdf(segment_cover_path, comp_title, dt_obj, seg_name, cat_name_cover, withdrawn_for_segment)
                         except Exception as e:
                             print(f"  Error creating segment cover for {prefix}: {e}")
                    
                    if os.path.exists(segment_cover_path):
                         file_list.append(segment_cover_path)
                    else:
                         print(f"  Warning: Could not create segment cover for {prefix}")
                else:
                    # Use Start List — apply strikethrough if there are withdrawn competitors
                    withdrawn_for_segment = prefix_withdrawn.get(prefix, [])
                    if withdrawn_for_segment:
                        strikethrough_path = f"{base_path}_StartListwithTimes_strikethrough.pdf"
                        if not os.path.exists(strikethrough_path):
                            create_start_list_with_strikethrough(
                                start_list_path, strikethrough_path, withdrawn_for_segment
                            )
                        if os.path.exists(strikethrough_path):
                            file_list.append(strikethrough_path)
                        else:
                            file_list.append(start_list_path)
                    else:
                        file_list.append(start_list_path)
                
                # Role specific files
                if role == "referee":
                    # Handle generic prefix for CalculationSetup
                    # Use the abbreviation length from the categories table to determine
                    # where the "category-specific" part of the prefix ends.
                    # For split groups (e.g. group 01), we need to preserve past the split digits.
                    abbrev_len = get_abbreviation_length(prefix, categories)
                    
                    calc_cut_pos = None
                    if abbrev_len is not None:
                        # Check if there's a split number after abbreviation + dashes
                        remainder = prefix[abbrev_len:]
                        stripped = remainder.lstrip("-")
                        if stripped and len(stripped) >= 2 and stripped[:2].isdigit():
                            # Preserve up to the split digits
                            dash_count = len(remainder) - len(stripped)
                            calc_cut_pos = abbrev_len + dash_count + 2
                        else:
                            calc_cut_pos = abbrev_len
                    elif len(prefix) >= 18:
                        calc_cut_pos = 18  # Fallback

                    if calc_cut_pos is not None and calc_cut_pos < len(prefix):
                        calc_prefix = prefix[:calc_cut_pos] + '-' * (len(prefix) - calc_cut_pos)
                    else:
                        calc_prefix = prefix  # Fallback
                        
                    file_list.append(os.path.join(segment_dir, f"{calc_prefix}_CalculationSetupVerificationforReferee.pdf"))
                    
                    file_list.append(f"{base_path}_RefereeSheet.pdf")
                    file_list.append(f"{base_path}_JudgesSheetAll_referee_{slug}.pdf")
                    
                elif role == "judge":
                    file_list.append(f"{base_path}_JudgesSheetAll_judge_{slug}.pdf")
                    
                elif role == "technical_controller":
                    file_list.append(sheet_without_withdrawn(base_path, "TechnicalControllerSheet"))
                    
                elif role == "technical_specialist_1":
                    file_list.append(sheet_without_withdrawn(base_path, "TechnicalSpecialistSheet1"))
                    
                elif role == "technical_specialist_2":
                    file_list.append(sheet_without_withdrawn(base_path, "TechnicalSpecialistSheet2"))
                    
                elif role in ["data_operator", "replay_operator"]:
                    file_list.append(f"{base_path}_PlannedProgramContent.pdf")
            
            # 3. Merge and Save
            output_filename = f"judgingpapers_{date_str}_{slug}.pdf"
            output_path = os.path.join(output_dir, output_filename)
            
            print(f"  Creating {output_filename}...")
            total_pages += merge_pdfs(output_path, file_list) or 0
            count += 1

    # ---------------------------------------------------------
    # 4. Combine Daily Papers
    # ---------------------------------------------------------
    print("\n[4/5] Combining Daily Papers...")
    
    for date_str, persons in person_tasks.items():
        daily_files = []
        sorted_slugs = sorted(persons.keys())
        
        for slug in sorted_slugs:
            filename = f"judgingpapers_{date_str}_{slug}.pdf"
            filepath = os.path.join(output_dir, filename)
            if os.path.exists(filepath):
                daily_files.append(filepath)
        
        if daily_files:
            daily_output_filename = f"judgingpapers_{date_str}.pdf"
            daily_output_path = os.path.join(output_dir, daily_output_filename)
            print(f"  Creating daily summary: {daily_output_filename} ({len(daily_files)} files)")
            merge_pdfs(daily_output_path, daily_files)

    # ---------------------------------------------------------
    # 5. Zip and Cleanup
    # ---------------------------------------------------------
    print("\n[5/5] Zipping and Cleaning up...")
    
    for date_str, persons in person_tasks.items():
        zip_filename = f"judgingPapers_{date_str}.zip"
        zip_path = os.path.join(output_dir, zip_filename)
        
        individual_files = []
        for slug in persons.keys():
            filename = f"judgingpapers_{date_str}_{slug}.pdf"
            filepath = os.path.join(output_dir, filename)
            if os.path.exists(filepath):
                individual_files.append((filepath, filename))
        
        if individual_files:
            print(f"  Creating zip: {zip_filename}")
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                 for filepath, arcname in individual_files:
                     zipf.write(filepath, arcname)
            
            # Remove individual files
            print(f"  Removing {len(individual_files)} individual files...")
            for filepath, _ in individual_files:
                try:
                    os.remove(filepath)
                except OSError as e:
                    print(f"  Error deleting {filepath}: {e}")

    print(f"\nDone! Created judging paper files and daily summaries in '{output_dir}'.")

    # ---------------------------------------------------------
    # 6. Compute usage statistics (best-effort, never raises)
    # ---------------------------------------------------------
    try:
        stats = _compute_statistics(
            categories, schedule_entries, person_tasks, prefix_withdrawn,
            language, total_pages, withdrawn_pages_removed
        )
    except Exception as e:
        print(f"  Warning: Failed to compute statistics: {e}")
        stats = {}

    return stats


def _compute_statistics(categories, schedule_entries, person_tasks, prefix_withdrawn,
                        language, total_pages, withdrawn_pages_removed=0):
    """
    Build a usage-statistics dict from the structures accumulated during
    process_judging_papers. Stored on the competitions table row (which
    survives soft-deletion) for a future statistics page.
    """
    # Distinct prefixes (segments) actually processed, from post-conflict-filter tasks
    prefixes = set()
    role_slugs = {}        # role bucket -> set of person slugs
    judge_slugs = set()    # slugs holding role 'judge'
    all_slugs = set()      # slugs across all roles
    judge_assignment_count = 0
    role_buckets = {
        'referee': 'referees',
        'technical_controller': 'technical_controllers',
        'technical_specialist_1': 'technical_specialists',
        'technical_specialist_2': 'technical_specialists',
        'data_operator': 'data_operators',
        'replay_operator': 'replay_operators',
    }

    for date_str, persons in person_tasks.items():
        for slug, data in persons.items():
            all_slugs.add(slug)
            for prefix, role in data['tasks']:
                prefixes.add(prefix)
                if role == 'judge':
                    judge_slugs.add(slug)
                    judge_assignment_count += 1
                elif role in role_buckets:
                    role_slugs.setdefault(role_buckets[role], set()).add(slug)

    officials_by_role = {bucket: len(slugs) for bucket, slugs in sorted(role_slugs.items())}

    # Categories / segment types / competition type / judging method per prefix
    category_names = set()
    competition_types = set()
    judging_methods = set()
    segment_types = set()
    for prefix in prefixes:
        matched = match_category(prefix, categories)
        if matched:
            if language == "en":
                category_names.add(matched["displayName"])
            else:
                category_names.add(matched.get("displayNameFi", matched["displayName"]))
            competition_types.add(matched["competitionType"])
            judging_methods.add(matched["judgingMethod"])
        _, segment = parse_prefix(prefix, categories, language=language)
        if segment and segment != "Category General":
            segment_types.add(segment)

    def _single_or_mixed(values):
        values = {v for v in values if v}
        if not values:
            return None
        return values.pop() if len(values) == 1 else "Mixed"

    # Competitor count: per category, the largest segment entry count from the
    # schedule (same field skates SP and FS), summed across categories.
    competitor_count = None
    if schedule_entries:
        per_category_max = {}
        for entry in schedule_entries:
            key = entry.get("category_name") or entry.get("event_name", "")
            per_category_max[key] = max(per_category_max.get(key, 0), entry.get("entries", 0))
        competitor_count = sum(per_category_max.values())

    # Competition days: prefer panel-derived dates, fall back to schedule dates
    dates = sorted(person_tasks.keys())
    if not dates and schedule_entries:
        dates = sorted({e["date"] for e in schedule_entries if e.get("date")})

    def _iso_date(yyyymmdd):
        return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:8]}" if len(yyyymmdd) == 8 else yyyymmdd

    # Withdrawn entries are counted per segment: a skater withdrawing from both
    # SP and FS counts twice (this measures withdrawn entries, not persons).
    withdrawn_count = sum(len(v) for v in prefix_withdrawn.values())

    return {
        "competition_type": _single_or_mixed(competition_types),
        "categories": sorted(category_names),
        "category_count": len(category_names),
        "competitor_count": competitor_count,
        "segment_count": len(prefixes),
        "segment_types": sorted(segment_types),
        "judge_assignment_count": judge_assignment_count,
        "unique_judge_count": len(judge_slugs),
        "unique_official_count": len(all_slugs),
        "officials_by_role": officials_by_role,
        "withdrawn_count": withdrawn_count,
        "withdrawn_pages_removed": withdrawn_pages_removed,
        "day_count": len(dates),
        "first_date": _iso_date(dates[0]) if dates else None,
        "last_date": _iso_date(dates[-1]) if dates else None,
        "judging_method": _single_or_mixed(judging_methods),
        "language": language,
        "pages_generated": total_pages,
    }
