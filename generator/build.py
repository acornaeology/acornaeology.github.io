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

        # Build version metadata for the index page
        versions = []
        for version_id in source["versions"]:
            version_dirpath = resolve_version_dirpath(repo_dirpath, version_id)
            if version_dirpath is None:
                print(f"  Warning: version directory not found for "
                      f"'{version_id}', skipping")
                continue
            rom_json_filepath = version_dirpath / "rom" / "rom.json"
            if rom_json_filepath.exists():
                rom_meta = json.loads(rom_json_filepath.read_text())
                title = rom_meta.get("title", f"{name} {version_id}")
                docs = [
                    {
                        "label": doc["label"],
                        "url": _doc_output_filename(version_id, doc["path"]),
                    }
                    for doc in rom_meta.get("docs", [])
                ]
            else:
                title = f"{name} {version_id}"
                docs = []
            versions.append({"id": version_id, "title": title, "docs": docs})

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

            links.append(report_link)

            sections = process_disassembly(data)

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


_ADDRESS_URI_HREF_RE = re.compile(
    r'<a href="address:([0-9A-Fa-f]{4,})(?:@([^"]+))?">',
    re.IGNORECASE,
)


def apply_address_uri_links(html, version_anchors, default_version=None,
                            source_label=""):
    """Rewrite Markdown-authored `address:HEX[@version]` URIs to anchors.

    Authors can write `[rx_frame_b](address:E263)` or
    `[rx_frame_b (&E263)](address:E263@3.60)` in Markdown sources;
    Markdown converts those to `<a href="address:E263[@version]">...</a>`.
    This post-processor resolves each such href to the matching
    disassembly page and anchor and rewrites it.

    - `version_anchors` is the per-version sorted-anchor dict built
      during the disassembly-rendering pass.
    - `default_version` supplies the version for unqualified URIs
      (omit `@version`). Pass the doc's own `version_id` inside
      per-version docs; pass `None` inside project-level analyses (where
      authors are required to be explicit).
    - `source_label` appears in warning messages so authors can find
      the offending source.

    Unresolvable references (no default, unknown version, no anchor
    at-or-before the address) print a warning and leave the `<a>` tag
    unchanged rather than crashing the build.
    """

    def rewrite(match):
        hex_str = match.group(1)
        version = match.group(2) or default_version
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

        return f'<a href="{version}.html#addr-{anchor:04X}">'

    return _ADDRESS_URI_HREF_RE.sub(rewrite, html)


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
