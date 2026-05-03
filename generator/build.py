#!/usr/bin/env python3
"""Static site builder for acornaeology.uk"""

import bisect
import json
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import quote

import markdown as markdown_lib
from jinja2 import Environment, FileSystemLoader
from markupsafe import Markup

from datetime import datetime

from .disassembly import process_disassembly
from .feed import generate_atom_feed, generate_sitemap
from .glossary import apply_glossary_links, build_glossary_lookup, parse_glossary


REPO_ROOT = Path(__file__).resolve().parent.parent
BASE_URL = "https://acornaeology.uk/"
SITE_DIRPATH = REPO_ROOT / "site"
TEMPLATES_DIRPATH = REPO_ROOT / "templates"
DATA_DIRPATH = REPO_ROOT / "data"
OUTPUT_DIRPATH = REPO_ROOT / "output"
CACHE_DIRPATH = REPO_ROOT / ".cache"


def git_last_modified_iso(repo_dirpath, target_dirpath):
    """Get the ISO 8601 author date of the latest commit touching target_dirpath."""
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%aI", "--", str(target_dirpath)],
            cwd=str(repo_dirpath),
            capture_output=True,
            text=True,
            check=True,
        )
        date_str = result.stdout.strip()
        return date_str if date_str else None
    except subprocess.CalledProcessError:
        return None


def format_display_date(iso_date):
    """Format an ISO 8601 date as '7 Mar 2026'."""
    dt = datetime.fromisoformat(iso_date)
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    return f"{dt.day} {months[dt.month - 1]} {dt.year}"


def resolve_version_dirpath(repo_dirpath, version_id):
    """Map a version ID to its prefixed directory.

    Searches for any subdirectory of versions/ whose name ends with
    '-{version_id}', allowing any prefix (nfs, anfs, tube-6502-client, etc.).
    """
    versions_dirpath = repo_dirpath / "versions"
    suffix = f"-{version_id}"
    for dirpath in sorted(versions_dirpath.iterdir()):
        if dirpath.is_dir() and dirpath.name.endswith(suffix):
            return dirpath
    return None


def is_page_template(filepath):
    """A page template extends a base — non-page templates are skipped.

    Templates prefixed with _ are data-driven and rendered separately."""
    if filepath.name.startswith("_"):
        return False
    content = filepath.read_text()
    return "{% extends" in content


def build_templates(env, roms, pages):
    """Render all page templates to the output directory."""
    for template_filepath in TEMPLATES_DIRPATH.glob("*.html"):
        if not is_page_template(template_filepath):
            continue
        template = env.get_template(template_filepath.name)
        output_filepath = OUTPUT_DIRPATH / template_filepath.name
        # 404 page can be served from any URL path, so use absolute root
        root = "/" if template_filepath.name == "404.html" else "./"
        html = template.render(root=root, roms=roms)
        output_filepath.write_text(html)
        print(f"  {template_filepath.name} -> {output_filepath.relative_to(REPO_ROOT)}")

        if template_filepath.name != "404.html":
            url = BASE_URL + template_filepath.name.replace("index.html", "")
            pages.append({"url": url})


def resolve_source(source):
    """Resolve a disassembly source to a local directory path.

    Uses the local path if available, otherwise clones the repo.
    """
    if "path" in source:
        local_dirpath = (DATA_DIRPATH / source["path"]).resolve()
        if local_dirpath.is_dir():
            return local_dirpath

    repo_url = source["repo"]
    # Derive a cache directory name from the repo URL
    repo_name = repo_url.rstrip("/").rsplit("/", 1)[-1]
    clone_dirpath = CACHE_DIRPATH / repo_name

    if clone_dirpath.is_dir():
        print(f"  Using cached clone: {clone_dirpath}")
        return clone_dirpath

    print(f"  Cloning {repo_url}...")
    CACHE_DIRPATH.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", repo_url, str(clone_dirpath)],
        check=True,
        capture_output=True,
    )
    return clone_dirpath


def load_sources():
    """Load and resolve all disassembly sources.

    Returns a list of dicts with manifest metadata and resolved repo path.
    """
    sources_filepath = DATA_DIRPATH / "sources.json"
    if not sources_filepath.exists():
        return []

    sources = json.loads(sources_filepath.read_text())
    result = []

    for source in sources:
        repo_dirpath = resolve_source(source)
        repo_url = source["repo"]

        manifest_filepath = repo_dirpath / "acornaeology.json"
        if not manifest_filepath.exists():
            print(f"  Warning: {manifest_filepath} not found, skipping")
            continue
        manifest = json.loads(manifest_filepath.read_text())

        description = manifest.get("description", "")
        # Extract first sentence for use on the index page.
        m = re.match(r'(.*?\.) (?=[A-Z]|$)', description)
        short_description = m.group(1) if m else description

        references = [
            {
                "label": ref["label"],
                "url": ref["url"],
                "icon": ref.get("icon", "ref"),
                "note": ref.get("note", ""),
            }
            for ref in manifest.get("references", [])
        ]

        # Project-level analyses: Markdown writeups about the ROM's
        # architecture or behaviour that aren't specific to a single
        # version. Each entry points to a .md file inside the source
        # repo and is rendered to <slug>/<stem>.html.
        analyses = [
            {
                "label": a["label"],
                "url": a["url"],
                "icon": a.get("icon", "doc"),
                "note": a.get("note", ""),
                "address_links": a.get("address_links", []),
                "glossary_links": a.get("glossary_links", []),
            }
            for a in manifest.get("analyses", [])
        ]

        result.append({
            "repo_dirpath": repo_dirpath,
            "repo_url": repo_url,
            "slug": manifest["slug"],
            "name": manifest["name"],
            "description": description,
            "short_description": short_description,
            "glossary": manifest.get("glossary"),
            "versions": manifest["versions"],
            "references": references,
            "analyses": analyses,
        })

    return result


def build_disassemblies(env, sources, pages):
    """Build disassembly pages from external disassembly repos."""
    rom_index_template = env.get_template("_rom_index.html")
    disassembly_template = env.get_template("_disassembly.html")

    for source in sources:
        repo_dirpath = source["repo_dirpath"]
        repo_url = source["repo_url"]
        slug = source["slug"]
        name = source["name"]
        description = source["description"]

        # Create output directory
        output_dirpath = OUTPUT_DIRPATH / slug
        output_dirpath.mkdir(parents=True, exist_ok=True)

        # Load and parse glossary if present
        glossary = None
        glossary_lookup = {}
        glossary_filepath_rel = source.get("glossary")
        if glossary_filepath_rel:
            glossary_filepath = repo_dirpath / glossary_filepath_rel
            if glossary_filepath.exists():
                glossary = parse_glossary(glossary_filepath.read_text())
                glossary_lookup = build_glossary_lookup(glossary)
            else:
                print(f"  Warning: glossary file {glossary_filepath} "
                      f"not found")

        # Build version metadata for the index page. A single peek at
        # each version's JSON tells us whether a per-version memory-map
        # page will be rendered later; the rom.json docs array has a
        # `"type": "changes"` marker distinguishing the changes-from doc
        # from any other per-version docs.
        versions = []
        for version_id in source["versions"]:
            version_dirpath = resolve_version_dirpath(repo_dirpath, version_id)
            if version_dirpath is None:
                print(f"  Warning: version directory not found for "
                      f"'{version_id}', skipping")
                continue

            output_json_dirpath = version_dirpath / "output"
            vjson_filepaths = list(output_json_dirpath.glob("*.json"))
            has_memory_map = False
            if vjson_filepaths:
                vdata = json.loads(vjson_filepaths[0].read_text())
                has_memory_map = bool(vdata.get("memory_map"))

            rom_json_filepath = version_dirpath / "rom" / "rom.json"
            if rom_json_filepath.exists():
                rom_meta = json.loads(rom_json_filepath.read_text())
                title = rom_meta.get("title", f"{name} {version_id}")
                doc_entries = rom_meta.get("docs", [])
            else:
                title = f"{name} {version_id}"
                doc_entries = []

            changes_doc = None
            other_docs = []
            for doc in doc_entries:
                if doc.get("type") == "changes":
                    changes_doc = doc
                else:
                    other_docs.append(doc)

            versions.append({
                "id": version_id,
                "title": title,
                "disassembly_url": f"{version_id}.html",
                "memory_map_url": (
                    f"{version_id}-memory-map.html" if has_memory_map else None
                ),
                "changes_url": (
                    _doc_output_filename(version_id, changes_doc["path"])
                    if changes_doc else None
                ),
                "changes_label": changes_doc["label"] if changes_doc else None,
                "other_docs": [
                    {
                        "label": d["label"],
                        "url": _doc_output_filename(version_id, d["path"]),
                    }
                    for d in other_docs
                ],
            })

        # Build per-ROM index page
        references = source.get("references", [])
        analyses_for_index = [
            {
                "label": a["label"],
                "url": _analysis_output_filename(a["url"]),
                "icon": a.get("icon", "doc"),
                "note": a.get("note", ""),
            }
            for a in source.get("analyses", [])
        ]
        html = rom_index_template.render(
            root="../",
            slug=slug,
            name=name,
            description=description,
            versions=versions,
            has_glossary=glossary is not None,
            analyses=analyses_for_index,
            references=references,
        )
        index_filepath = output_dirpath / "index.html"
        index_filepath.write_text(html)
        print(f"  {slug}/index.html")
        pages.append({"url": f"{BASE_URL}{slug}/"})

        # Build glossary page if glossary data exists
        if glossary:
            _render_glossary_page(env, slug, name, glossary, output_dirpath,
                                  pages)

        # Build per-version disassembly pages
        version_anchors = {}  # version_id -> sorted list of anchor addresses
        for version_id in source["versions"]:
            version_dirpath = resolve_version_dirpath(repo_dirpath, version_id)
            if version_dirpath is None:
                print(f"  Warning: version directory not found for "
                      f"'{version_id}', skipping")
                continue

            # Get last-modified date from git history
            updated_iso = git_last_modified_iso(repo_dirpath, version_dirpath)
            updated_display = format_display_date(updated_iso) if updated_iso else None

            # Find the disassembly JSON
            output_json_dirpath = version_dirpath / "output"
            json_files = list(output_json_dirpath.glob("*.json"))
            if not json_files:
                print(f"  Warning: no JSON found in {output_json_dirpath}, skipping")
                continue
            data_filepath = json_files[0]
            data = json.loads(data_filepath.read_text())

            # Collect valid anchor addresses for this version
            anchors = set()
            for item in data["items"]:
                anchors.add(item["addr"])
                if "binary_addr" in item:
                    anchors.add(item["binary_addr"])
            version_anchors[version_id] = sorted(anchors)

            # Read version metadata
            rom_json_filepath = version_dirpath / "rom" / "rom.json"
            if rom_json_filepath.exists():
                rom_meta = json.loads(rom_json_filepath.read_text())
                title = rom_meta.get("title", f"{name} {version_id}")
                links = list(rom_meta.get("links", []))
            else:
                title = f"{name} {version_id}"
                links = []

            # Prepend a GitHub link unless rom.json already supplies one
            if not any(l.get("icon") == "github" for l in links):
                links.insert(0, {
                    "label": "Disassembly source on GitHub",
                    "url": repo_url,
                    "icon": "github",
                })

            # Add report-issue link with prefilled title and body
            issue_title = f"[{title}] "
            page_url = f"{BASE_URL}{slug}/{version_id}.html"
            issue_body = (
                f"**Version:** {title}\n"
                f"**Page:** {page_url}\n"
                f"**Memory address:** \n\n"
                "---\n\n"
                "\n\n"
                "---\n"
                "_Tip: to link to a specific address, hover over an address "
                "in the listing and click the link icon to copy a permalink, "
                "then paste it in the **Memory address** field above._\n"
            )
            issue_url = (
                f"{repo_url}/issues/new"
                f"?title={quote(issue_title)}"
                f"&body={quote(issue_body)}"
            )
            report_link = {
                "label": "Found a mistake or a comment that could be clearer? Report an issue.",
                "url": issue_url,
                "icon": "bug",
            }

            # Append doc links
            for doc in rom_meta.get("docs", []):
                links.append({
                    "label": doc["label"],
                    "url": _doc_output_filename(version_id, doc["path"]),
                    "icon": "doc",
                })

            # Append memory-map link if this version has one. The page is
            # rendered later (line ~430) but its presence is determined
            # by whether the disassembly JSON carries memory_map entries.
            # target="memory-map" pairs the page with the named window
            # so the side-by-side memory-map / listing pattern works.
            # class="mm-link" hooks the link into the listing-page
            # JavaScript that focuses an already-open memory-map tab,
            # matching the behaviour of inline address: links.
            if data.get("memory_map"):
                links.append({
                    "label": "Memory map",
                    "url": f"{version_id}-memory-map.html",
                    "icon": "map",
                    "target": "memory-map",
                    "class": "mm-link",
                })

            links.append(report_link)

            sections = process_disassembly(data, version_id=version_id)

            html = disassembly_template.render(
                root="../",
                slug=slug,
                name=name,
                version_id=version_id,
                title=title,
                description=description,
                links=links,
                sections=sections,
                subroutines=_filter_subroutines(data),
                updated_iso=updated_iso,
                updated_display=updated_display,
            )

            version_filepath = output_dirpath / f"{version_id}.html"
            version_filepath.write_text(html)
            print(f"  {slug}/{version_id}.html")
            pages.append({
                "url": f"{BASE_URL}{slug}/{version_id}.html",
                "title": title,
                "description": description,
                "is_disassembly": True,
                "updated": updated_iso,
            })

            # Build doc pages for this version
            _render_doc_pages(env, source, version_id, version_dirpath,
                              rom_meta, output_dirpath, version_anchors,
                              glossary_lookup, pages)

            # Build the memory-map page for this version (if the driver
            # enriched any non-ROM labels with memory-map metadata).
            mm_entries = data.get("memory_map", [])
            if mm_entries:
                group_titles = rom_meta.get("memory_map_groups", {})
                _render_memory_map_page(env, source, version_id, title,
                                        mm_entries, output_dirpath,
                                        version_anchors, pages,
                                        group_titles=group_titles)

        # Build project-level analysis pages (after all versions, so
        # analyses can link into any version's anchors)
        _render_analysis_pages(env, source, output_dirpath,
                               version_anchors, glossary_lookup, pages)


def _doc_output_filename(version_id, doc_path):
    """Derive the output HTML filename for a doc entry."""
    stem = Path(doc_path).stem.lower()
    return f"{version_id}-{stem}.html"


def _analysis_output_filename(analysis_url):
    """Derive the output HTML filename for a project-level analysis.

    No version prefix: analyses are project-level, so the filename is
    just the Markdown stem. Collisions between analyses with the same
    stem must be avoided by the source manifest.
    """
    stem = Path(analysis_url).stem.lower()
    return f"{stem}.html"


def _resolve_anchor(address, anchors):
    """Find the nearest preceding valid anchor for an address.

    If the exact address has an anchor, return it unchanged. Otherwise
    return the largest anchor address that precedes it.
    """
    pos = bisect.bisect_left(anchors, address)
    if pos < len(anchors) and anchors[pos] == address:
        return address
    if pos > 0:
        return anchors[pos - 1]
    return address


_ADDRESS_URI_RE = re.compile(
    r'<a href="address:'
    r'([0-9A-Fa-f]{4,})'     # hex address
    r'(?:@([^"?]+))?'        # optional @version
    r'(?:\?([^"]*))?'        # optional ?flag1[&flag2...]
    r'">'
    r'(.*?)'                 # label (markdown-converted, may contain HTML)
    r'</a>',
    re.IGNORECASE | re.DOTALL,
)


def apply_address_uri_links(html, version_anchors, default_version=None,
                            source_label="", link_class=None, target=None):
    """Rewrite Markdown-authored `address:HEX[@version][?flag]` URIs.

    Authors write inline links in their Markdown sources such as

        [rx_frame_b](address:E263)                  — label-only link
        [rx_frame_b](address:E263?hex)              — append " (&E263)"
        [rx_frame_b](address:E263@3.60?hex)         — explicit version
        [rx_frame_b (&E263)](address:E263@3.60)     — author wrote label
                                                      fully by hand

    Markdown converts those to `<a href="address:…">label</a>`; this
    post-processor resolves each URI to the matching disassembly page
    and anchor and rewrites the output.

    Flags (after `?`) supported:

    - `hex` — append ` (&<HEX>)` as a second hyperlink to the same
      anchor, with the hex formatted in `<code>` for visual parity
      with backticked labels. The space between the two links and the
      enclosing parentheses are deliberately outside the `<a>` tags so
      only the label and the hex itself are clickable.

    - `version_anchors` is the per-version sorted-anchor dict built
      during the disassembly-rendering pass.
    - `default_version` supplies the version for unqualified URIs
      (omit `@version`). Pass the doc's own `version_id` inside
      per-version docs; pass `None` inside project-level analyses.
    - `source_label` appears in warning messages so authors can find
      the offending source.
    - `link_class` and `target` are injected as attributes on every
      emitted `<a>`. Callers pass `link_class="listing-link"` +
      `target="listing"` from the memory-map page so listing links
      open in a named side-window that pairs with the memory-map
      window for side-by-side reading.

    Unresolvable references (no default, unknown version, no anchor
    at-or-before the address, unknown flag) print a warning and leave
    the `<a>` tag unchanged rather than crashing the build.
    """

    extra_attrs = ""
    if link_class:
        extra_attrs += f' class="{link_class}"'
    if target:
        extra_attrs += f' target="{target}"'

    def rewrite(match):
        hex_str, version, flag, label = match.groups()
        version = version or default_version
        src = f" ({source_label})" if source_label else ""

        if version is None:
            print(f"  Warning: address:{hex_str} has no @version qualifier "
                  f"and no default is available here{src}")
            return match.group(0)

        if version not in version_anchors:
            print(f"  Warning: address:{hex_str}@{version} — unknown "
                  f"version{src}")
            return match.group(0)

        addr = int(hex_str, 16)
        anchors_sorted = version_anchors[version]
        pos = bisect.bisect_left(anchors_sorted, addr)
        if pos < len(anchors_sorted) and anchors_sorted[pos] == addr:
            anchor = addr
        elif pos > 0:
            anchor = anchors_sorted[pos - 1]
        else:
            print(f"  Warning: address:{hex_str}@{version} — no anchor at "
                  f"or before &{hex_str}{src}")
            return match.group(0)

        url = f"{version}.html#addr-{anchor:04X}"

        if not flag:
            return f'<a{extra_attrs} href="{url}">{label}</a>'

        if flag.lower() == "hex":
            hex_display = f'<code>&amp;{hex_str.upper()}</code>'
            return (f'<a{extra_attrs} href="{url}">{label}</a> '
                    f'(<a{extra_attrs} href="{url}">{hex_display}</a>)')

        print(f"  Warning: address:{hex_str}@{version} — unknown flag "
              f"'?{flag}'{src}")
        return match.group(0)

    return _ADDRESS_URI_RE.sub(rewrite, html)


def _apply_address_links(md_text, address_links, version_anchors=None):
    """Insert Markdown links for address references before HTML conversion.

    Each entry in address_links specifies a pattern to match, which
    occurrence to link, and the target version/address for the anchor.
    Replacements are applied end-to-start so positions don't shift.

    If version_anchors is provided, addresses that don't have a direct
    anchor are resolved to the nearest preceding anchor address.
    """
    replacements = []

    for link_spec in address_links:
        pattern = link_spec["pattern"]
        occurrence = link_spec["occurrence"]
        version = link_spec["version"]
        address = int(link_spec["address"], 0)

        anchor_addr = address
        if version_anchors and version in version_anchors:
            anchor_addr = _resolve_anchor(address, version_anchors[version])

        url = f"{version}.html#addr-{anchor_addr:04X}"

        matches = list(re.finditer(pattern, md_text))
        if not matches:
            print(f"  Warning: pattern '{pattern}' not found in doc")
            continue

        idx = occurrence if occurrence >= 0 else len(matches) + occurrence
        if idx < 0 or idx >= len(matches):
            print(f"  Warning: occurrence {occurrence} out of range "
                  f"for pattern '{pattern}'")
            continue

        match = matches[idx]
        replacement = f"[{match.group(0)}]({url})"
        replacements.append((match.start(), match.end(), replacement))

    replacements.sort(key=lambda r: r[0], reverse=True)
    for start, end, replacement in replacements:
        md_text = md_text[:start] + replacement + md_text[end:]

    return md_text


def _render_glossary_page(env, slug, name, glossary, output_dirpath, pages):
    """Build the glossary page from parsed glossary data."""
    glossary_template = env.get_template("_glossary.html")

    # Convert preamble markdown to HTML
    preamble_html = ""
    if glossary["preamble"]:
        converter = markdown_lib.Markdown()
        preamble_html = Markup(converter.convert(glossary["preamble"]))

    # Convert brief and extended text to HTML for each term
    for category in glossary["categories"]:
        for entry in category["terms"]:
            converter = markdown_lib.Markdown()
            brief_html = converter.convert(entry["brief"])
            if brief_html.startswith("<p>") and brief_html.endswith("</p>"):
                brief_html = brief_html[3:-4]
            entry["brief_html"] = Markup(brief_html)

            if entry["extended"]:
                converter = markdown_lib.Markdown()
                entry["extended_html"] = Markup(
                    converter.convert(entry["extended"]))
            else:
                entry["extended_html"] = None

    html = glossary_template.render(
        root="../",
        slug=slug,
        name=name,
        preamble=preamble_html,
        categories=glossary["categories"],
    )
    glossary_output_filepath = output_dirpath / "glossary.html"
    glossary_output_filepath.write_text(html)
    print(f"  {slug}/glossary.html")
    pages.append({"url": f"{BASE_URL}{slug}/glossary.html"})


def _render_doc_pages(env, source, version_id, version_dirpath, rom_meta,
                      output_dirpath, version_anchors=None,
                      glossary_lookup=None, pages=None):
    """Build document pages declared in rom.json for this version."""
    doc_template = env.get_template("_doc.html")
    name = source["name"]

    for doc in rom_meta.get("docs", []):
        md_filepath = version_dirpath / doc["path"]
        if not md_filepath.exists():
            print(f"  Warning: doc file {md_filepath} not found, skipping")
            continue

        md_text = md_filepath.read_text()

        address_links = doc.get("address_links", [])
        if address_links:
            md_text = _apply_address_links(md_text, address_links,
                                           version_anchors)

        converter = markdown_lib.Markdown(extensions=["tables", "fenced_code"])
        content_html = converter.convert(md_text)

        # Apply glossary links (post-HTML-conversion)
        glossary_links = doc.get("glossary_links", [])
        if glossary_links and glossary_lookup:
            content_html = apply_glossary_links(
                content_html, glossary_links, glossary_lookup,
                source["slug"])

        doc_filename = _doc_output_filename(version_id, doc["path"])

        # Rewrite inline [label](address:HEX[@version]) URIs. Unqualified
        # URIs default to this doc's own version.
        content_html = apply_address_uri_links(
            content_html, version_anchors,
            default_version=version_id,
            source_label=f"{source['slug']}/{doc_filename}")

        disassembly_title = rom_meta.get("title", f"{name} {version_id}")
        html = doc_template.render(
            root="../",
            slug=source["slug"],
            doc_filename=doc_filename,
            version_id=version_id,
            title=doc["label"],
            description=source["description"],
            content=Markup(content_html),
            back_url=f"{version_id}.html",
            back_label=f"{disassembly_title} disassembly",
        )

        output_filepath = output_dirpath / doc_filename
        output_filepath.write_text(html)
        print(f"  {source['slug']}/{doc_filename}")
        if pages is not None:
            pages.append({
                "url": f"{BASE_URL}{source['slug']}/{doc_filename}",
            })


def _rewrite_md_links_to_html(html, analyses):
    """Rewrite href="<stem>.md" to href="<stem>.html" for every
    analysis in the project.

    Each analysis becomes a flat `<slug>/<stem>.html` page, so any
    sibling-analysis link of the form `href="<stem>.md"` (same
    directory) or `href="../analysis/<stem>.md"` (the GitHub-style
    path we often see in Markdown sources) needs to point at the
    rendered sibling. We only rewrite links whose basename stem
    matches one of the rendered analyses, leaving unrelated `.md`
    URLs (e.g. external GitHub links) alone.
    """
    known_stems = {Path(a["url"]).stem.lower() for a in analyses}
    if not known_stems:
        return html

    def repl(match):
        href = match.group(1)
        stem = Path(href).stem.lower()
        if stem in known_stems:
            return f'href="{stem}.html"'
        return match.group(0)

    # Match href="..." where the target ends in .md and isn't an
    # absolute URL (no scheme, no leading //).
    return re.sub(r'href="((?!https?:|//)[^"#?]+\.md)(?:#[^"]*)?"', repl, html)


def _render_analysis_pages(env, source, output_dirpath,
                           version_anchors=None, glossary_lookup=None,
                           pages=None):
    """Build project-level analysis pages from acornaeology.json.

    Each entry in `source["analyses"]` points to a Markdown file
    inside the source repo and is rendered to a standalone HTML
    page at `<slug>/<stem>.html`. `address_links` and
    `glossary_links` behave as for per-version docs.
    """
    analyses = source.get("analyses", [])
    if not analyses:
        return

    doc_template = env.get_template("_doc.html")
    repo_dirpath = source["repo_dirpath"]
    slug = source["slug"]

    for analysis in analyses:
        md_filepath = repo_dirpath / analysis["url"]
        if not md_filepath.exists():
            print(f"  Warning: analysis file {md_filepath} not found, skipping")
            continue

        md_text = md_filepath.read_text()

        address_links = analysis.get("address_links", [])
        if address_links:
            md_text = _apply_address_links(md_text, address_links,
                                           version_anchors)

        converter = markdown_lib.Markdown(extensions=["tables", "fenced_code"])
        content_html = converter.convert(md_text)

        # Rewrite inter-analysis links: the Markdown sources link to
        # sibling writeups as `foo.md` (a valid GitHub link) but the
        # site renders each as `foo.html`. Remap same-directory
        # `.md` hrefs to `.html` so the rendered pages are
        # navigable.
        content_html = _rewrite_md_links_to_html(content_html, analyses)

        glossary_links = analysis.get("glossary_links", [])
        if glossary_links and glossary_lookup:
            content_html = apply_glossary_links(
                content_html, glossary_links, glossary_lookup, slug)

        analysis_filename = _analysis_output_filename(analysis["url"])

        # Rewrite inline [label](address:HEX[@version]) URIs. Analyses
        # are project-level, so there's no implicit "current version";
        # authors must always specify @version. apply_address_uri_links
        # warns on unqualified references rather than guessing.
        content_html = apply_address_uri_links(
            content_html, version_anchors,
            default_version=None,
            source_label=f"{slug}/{analysis_filename}")
        html = doc_template.render(
            root="../",
            slug=slug,
            doc_filename=analysis_filename,
            version_id=None,
            title=analysis["label"],
            description=source["description"],
            content=Markup(content_html),
            back_url="index.html",
            back_label=source["name"],
        )

        output_filepath = output_dirpath / analysis_filename
        output_filepath.write_text(html)
        print(f"  {slug}/{analysis_filename}")
        if pages is not None:
            pages.append({
                "url": f"{BASE_URL}{slug}/{analysis_filename}",
            })




def _render_memory_map_page(env, source, version_id, version_title,
                            memory_map, output_dirpath, version_anchors,
                            pages=None, group_titles=None):
    """Render {version_id}-memory-map.html for one version of a project.

    `memory_map` is the list of entries produced by py8dis's
    `structured.emit_structured()["memory_map"]` for this specific
    version. Each entry is:

        {addr, name, [length, group, access, description]}

    The memory map is version-scoped because workspace layout can shift
    between ROM releases. Descriptions pass through the standard
    Markdown pipeline; `[label](address:HEX)` links resolve to either
    `{version_id}.html#addr-XXXX` (ROM code) or `#mm-NAME` (other
    entries on the same memory-map page).

    `group_titles` maps memory-map group keys (e.g. `zero_page`,
    `hazel`) to display titles. Sourced directly from this version's
    `rom.json` `memory_map_groups` field. Unmapped groups fall back
    to the title-cased key.
    """
    if group_titles is None:
        group_titles = {}
    if not memory_map:
        return

    # Preserve the first-seen order of groups, rather than alphabetising,
    # so authors' mental ordering (ZP -> workspace -> buffers -> MMIO)
    # survives.
    group_order = []
    group_entries = {}
    for entry in memory_map:
        g = entry.get("group") or "other"
        if g not in group_entries:
            group_order.append(g)
            group_entries[g] = []
        group_entries[g].append(entry)

    # Map of memory-map addresses -> entry name, so descriptions that
    # cross-reference another memory-map entry (e.g. mem_ptr_lo mentions
    # mem_ptr_hi via `address:0081`) resolve to the entry's in-page
    # `#mm-NAME` anchor instead of falling through to the ROM-range
    # resolver (which would warn and leave the tag unchanged).
    mm_addr_to_name = {e["addr"]: e["name"] for e in memory_map}

    def rewrite_mm_refs(html):
        def repl(match):
            hex_str, version, flag, label = match.groups()
            # Only intercept unqualified URIs (no @version). Explicitly
            # versioned URIs still go to the ROM-range resolver, which
            # also handles multi-version anchor disambiguation.
            if version:
                return match.group(0)
            addr = int(hex_str, 16)
            name = mm_addr_to_name.get(addr)
            if name is None:
                return match.group(0)
            url = f"#mm-{name}"
            # `mm-link` is the cyan "memory-location reference" class;
            # used here for same-page jumps between memory-map entries
            # and on the listing page for cross-window navigation. The
            # shared class gives the two views a consistent palette:
            # memory locations read cyan regardless of which page the
            # reader is on.
            if not flag:
                return f'<a class="mm-link" href="{url}">{label}</a>'
            if flag.lower() == "hex":
                hex_display = f'<code>&amp;{hex_str.upper()}</code>'
                return (f'<a class="mm-link" href="{url}">{label}</a> '
                        f'(<a class="mm-link" href="{url}">{hex_display}</a>)')
            return match.group(0)
        return _ADDRESS_URI_RE.sub(repl, html)

    output_filename = f"{version_id}-memory-map.html"

    def render_description(md):
        if not md:
            return Markup("")
        # Use the cross-page Markdown pipeline (same as analyses/docs):
        # mistletoe-on-listing emits same-page `#addr-XXXX`, but here we
        # want `{version_id}.html#addr-XXXX` for ROM refs and `#mm-NAME`
        # for intra-memory-map cross-references.
        converter = markdown_lib.Markdown(extensions=["tables", "fenced_code"])
        html = converter.convert(md)
        html = rewrite_mm_refs(html)
        html = apply_address_uri_links(
            html, version_anchors,
            default_version=version_id,
            source_label=f"{source['slug']}/{output_filename}",
            link_class="listing-link",
            target="listing")
        return Markup(html)

    def access_display(v):
        if not v:
            return ""
        return {"r": "R", "w": "W", "rw": "R/W"}.get(v.lower(), v.upper())

    def addr_display(entry):
        start = entry["addr"]
        length = entry.get("length") or 1
        if length <= 1:
            return f"&{start:04X}"
        return f"&{start:04X}–&{start + length - 1:04X}"

    groups = []
    for g in group_order:
        entries = [
            {
                "addr_display": addr_display(e),
                "name": e["name"],
                "access_display": access_display(e.get("access")),
                "description_html": render_description(e.get("description")),
            }
            for e in group_entries[g]
        ]
        groups.append({
            "name": group_titles.get(g, g.replace("_", " ").title()),
            "slug": g.replace("_", "-"),
            "entries": entries,
        })

    template = env.get_template("_memory_map.html")
    html = template.render(
        root="../",
        slug=source["slug"],
        name=source["name"],
        version_id=version_id,
        title=version_title,
        output_filename=output_filename,
        groups=groups,
    )
    (output_dirpath / output_filename).write_text(html)
    print(f"  {source['slug']}/{output_filename}")
    if pages is not None:
        pages.append({"url": f"{BASE_URL}{source['slug']}/{output_filename}"})


def _filter_subroutines(data):
    """Return only subroutines within the ROM address range."""
    meta = data.get("meta", {})
    load_addr = meta.get("load_addr", 0)
    end_addr = meta.get("end_addr", 0xFFFF)
    return [
        s for s in data.get("subroutines", [])
        if load_addr <= s["addr"] < end_addr
    ]


def copy_static():
    """Copy static assets and top-level files to the output directory."""
    for subdir in ("css", "fonts", "images"):
        src = SITE_DIRPATH / subdir
        dst = OUTPUT_DIRPATH / subdir
        if src.is_dir():
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
            print(f"  {subdir}/")
    for filepath in SITE_DIRPATH.iterdir():
        if filepath.is_file():
            shutil.copy2(filepath, OUTPUT_DIRPATH / filepath.name)
            print(f"  {filepath.name}")


def main():
    print("Building acornaeology.uk...")

    # Clean output
    if OUTPUT_DIRPATH.exists():
        shutil.rmtree(OUTPUT_DIRPATH)
    OUTPUT_DIRPATH.mkdir()

    # Set up Jinja2
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIRPATH)),
        autoescape=True,
    )

    # Copy static assets
    print("Static assets:")
    copy_static()

    # Load disassembly sources
    sources = load_sources()

    # Track all pages for sitemap and feed
    pages = []

    # Render templates
    print("Pages:")
    build_templates(env, sources, pages)

    # Build disassembly pages
    print("Disassemblies:")
    build_disassemblies(env, sources, pages)

    # Generate sitemap and feed
    print("Feeds:")
    generate_sitemap(pages, OUTPUT_DIRPATH / "sitemap.xml")
    print("  sitemap.xml")
    generate_atom_feed(pages, OUTPUT_DIRPATH / "atom.xml", BASE_URL)
    print("  atom.xml")

    print("Done.")


if __name__ == "__main__":
    main()
