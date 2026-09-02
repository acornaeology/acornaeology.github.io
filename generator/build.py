#!/usr/bin/env python3
"""Static site builder for acornaeology.uk"""

import bisect
import json
import re
import shutil
import subprocess
from html import escape
from pathlib import Path
from urllib.parse import quote

import markdown as markdown_lib
from jinja2 import Environment, FileSystemLoader
from markupsafe import Markup

from datetime import datetime

from .disassembly import build_label_addrs, build_macros, process_disassembly
from .feed import generate_atom_feed, generate_sitemap
from .glossary import build_glossary_lookup, parse_glossary


REPO_ROOT = Path(__file__).resolve().parent.parent
BASE_URL = "https://acornaeology.uk/"
SITE_DIRPATH = REPO_ROOT / "site"
TEMPLATES_DIRPATH = REPO_ROOT / "templates"
DATA_DIRPATH = REPO_ROOT / "data"
OUTPUT_DIRPATH = REPO_ROOT / "output"
CACHE_DIRPATH = REPO_ROOT / ".cache"

# The disassembly-page link list is grouped so a reader can tell our own
# artefacts from the ROM's provenance, the upstream work we actually
# consulted, and community discussion — making research-source
# attribution explicit rather than a flat, icon-only list. Groups render
# in this order under these headings; empty groups are omitted.
LINK_CATEGORIES = [
    ("ours", "This disassembly"),
    ("rom-image", "ROM image"),
    ("source", "Sources consulted"),
    ("related", "Further reading"),
    ("discussion", "Discussion"),
    ("feedback", "Feedback"),
]
_LINK_CATEGORY_KEYS = {key for key, _ in LINK_CATEGORIES}

# A link may declare its `category` explicitly in rom.json. When it
# doesn't, fall back to inferring one from its icon so un-migrated
# rom.json files still group correctly (staggered rollout, as with the
# dasmos schema version). `ref`/`doc` default to "source" — the honest
# assumption is that a referenced disassembly or document informed the
# annotations; an author moves it to "related" to mark it as further
# reading not directly consulted.
_ICON_LINK_CATEGORY = {
    "github": "ours",
    "map": "ours",
    "chip": "rom-image",
    "ref": "source",
    "doc": "source",
    "code": "ours",
    "chat": "discussion",
    "bug": "feedback",
}
DEFAULT_LINK_CATEGORY = "related"

# Toolchain attribution, credited site-wide in the footer.
DASMOS_URL = "https://github.com/acornaeology/dasmos"


def _link_category(link):
    """Resolve a link's category: explicit `category`, else icon-inferred.

    An explicit category that isn't one of the known keys is coerced to
    the default so a typo surfaces as a visible group rather than a
    dropped link.
    """
    category = link.get("category")
    if category in _LINK_CATEGORY_KEYS:
        return category
    if category:
        return DEFAULT_LINK_CATEGORY
    return _ICON_LINK_CATEGORY.get(link.get("icon"), DEFAULT_LINK_CATEGORY)


def _build_source_link(rom_meta, repo_url):
    """Build the single "This disassembly" source link.

    A version's `rom.json` may list its assembler flavours under
    `sources` (each `{"assembler", "url"}`); they render as one line —
    "Disassembly source on GitHub (beebasm, 64tass)" — with each flavour
    linked. Falls back to `rom.json`'s legacy single github link, or an
    injected link to the repo root when neither is present.
    """
    sources = rom_meta.get("sources")
    if sources:
        return {
            "label": "Disassembly source on GitHub",
            "icon": "github",
            "category": "ours",
            "variants": [
                {"label": s.get("assembler") or s.get("label") or "source",
                 "url": s["url"]}
                for s in sources
            ],
        }
    legacy = next((l for l in rom_meta.get("links", [])
                   if l.get("icon") == "github"), None)
    if legacy:
        return {**legacy, "category": legacy.get("category", "ours")}
    return {
        "label": "Disassembly source on GitHub",
        "url": repo_url,
        "icon": "github",
        "category": "ours",
    }


def _merge_references(shared, version_refs):
    """Merge repo-level (shared) references with a version's own.

    Shared references are the default for every version. A version-level
    reference (from its `rom.json`) either *specialises* a shared one —
    when its `id` matches, it replaces that entry in place (e.g. swapping
    a Model-B MOS disassembly for the Master one) — or *enriches* the
    list, when it has a new/absent `id`, by appending. A version entry
    with a matching `id` and `suppress: true` removes the shared one
    without a replacement. Shared references keep their position.
    """
    result = list(shared)
    index_by_id = {r["id"]: i for i, r in enumerate(result) if r.get("id")}
    for vref in version_refs:
        rid = vref.get("id")
        if rid is not None and rid in index_by_id:
            i = index_by_id[rid]
            result[i] = None if vref.get("suppress") else vref
        else:
            result.append(vref)
    return [r for r in result if r is not None]


def _dedup_links(links):
    """Drop later links whose URL already appeared (first occurrence wins).

    Lets version-specific `rom.json` links and repo-level manifest
    `references` be concatenated without the shared entries (a ROM-image
    link, a discussion thread) showing twice.
    """
    seen = set()
    out = []
    for link in links:
        url = link.get("url")
        if url and url in seen:
            continue
        if url:
            seen.add(url)
        out.append(link)
    return out


def _group_links(links):
    """Bucket a flat link list into ordered `LINK_CATEGORIES` groups.

    Returns a list of `{category, heading, links}` for each non-empty
    group, preserving each link's original order within its group.
    """
    buckets = {key: [] for key, _ in LINK_CATEGORIES}
    for link in links:
        buckets[_link_category(link)].append(link)
    return [
        {"category": key, "heading": heading, "links": buckets[key]}
        for key, heading in LINK_CATEGORIES
        if buckets[key]
    ]


def _index_references(source):
    """Aggregate the references shown on a ROM's index page.

    The index is a repo-level bibliography, so it unions the shared
    references with each version's own `rom.json` references and
    discussion links — family-specific threads (a NFS thread vs an ANFS
    thread) live per-version, not in the manifest, but should still all
    appear on the shared index. Deduped by URL. Per-version identity
    links (ROM image, source) stay off the index; they belong on the
    version pages and in the version list.
    """
    version_discussion = []
    version_refs = []
    repo_dirpath = source["repo_dirpath"]
    for version_id in source.get("versions", []):
        version_dirpath = resolve_version_dirpath(repo_dirpath, version_id)
        if version_dirpath is None:
            continue
        meta_filepath = _resolve_version_meta_filepath(version_dirpath)
        if not meta_filepath.exists():
            continue
        rom_meta = json.loads(meta_filepath.read_text())
        version_refs.extend(rom_meta.get("references", []))
        version_discussion.extend(
            link for link in rom_meta.get("links", [])
            if _link_category(link) == "discussion")
    # Order so that each category reads naturally once grouped: the
    # per-disassembly "Discuss this…" threads lead Discussion (version
    # discussion first), while the shared primary sources lead Sources
    # consulted (shared references before version-specific ones).
    return _dedup_links(
        version_discussion + list(source.get("references", [])) + version_refs)


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
                "category": ref.get("category"),
                # Optional stable key. A version-level reference in
                # rom.json with the same `id` specialises (overrides) this
                # shared one — e.g. swapping the Model-B MOS disassembly
                # for the Master one on a Master-only version.
                "id": ref.get("id"),
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
        glossary_slug_lookup = {}
        glossary_filepath_rel = source.get("glossary")
        if glossary_filepath_rel:
            glossary_filepath = repo_dirpath / glossary_filepath_rel
            if glossary_filepath.exists():
                glossary = parse_glossary(glossary_filepath.read_text())
                glossary_lookup = build_glossary_lookup(glossary)
                # Slug-keyed view for the `glossary:SLUG` link scheme.
                glossary_slug_lookup = {
                    e["slug"]: e for e in glossary_lookup.values()}
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

            meta_filepath = _resolve_version_meta_filepath(version_dirpath)
            if meta_filepath.exists():
                rom_meta = json.loads(meta_filepath.read_text())
                title = rom_meta.get("title", f"{name} {version_id}")
                doc_entries = rom_meta.get("docs", [])
                source_file_entries = rom_meta.get("source_files", [])
            else:
                rom_meta = {}
                title = f"{name} {version_id}"
                doc_entries = []
                source_file_entries = []

            primary_json_filepath = _primary_disassembly_json(
                version_dirpath, rom_meta)
            has_memory_map = False
            if primary_json_filepath is not None:
                vdata = json.loads(primary_json_filepath.read_text())
                has_memory_map = bool(
                    vdata.get("memory_map") or vdata.get("index_bases"))

            # Additional disassembly listings (e.g. driver variants) — each
            # rendered as its own formatted page and listed under the parent
            # program in the index.
            additional_disassemblies = []
            for extra in rom_meta.get("disassemblies", []):
                extra_page_id = f"{version_id}-{extra['slug']}"
                extra_has_mm = False
                extra_json_filepath = version_dirpath / extra["json"]
                if extra_json_filepath.exists():
                    extra_data = json.loads(extra_json_filepath.read_text())
                    extra_has_mm = bool(
                        extra_data.get("memory_map")
                        or extra_data.get("index_bases"))
                additional_disassemblies.append({
                    "label": extra["label"],
                    "disassembly_url": f"{extra_page_id}.html",
                    "memory_map_url": (
                        f"{extra_page_id}-memory-map.html"
                        if extra_has_mm else None
                    ),
                })

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
                "source_files": [
                    {
                        "label": sf["label"],
                        "url": _source_output_filename(version_id, sf),
                        "language": sf.get("language", ""),
                    }
                    for sf in source_file_entries
                ],
                "additional_disassemblies": additional_disassemblies,
            })

        # Build per-ROM index page
        reference_groups = _group_links(_index_references(source))
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
            reference_groups=reference_groups,
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
        version_mm_links = {}  # version_id -> {addr: "{version}-memory-map.html#mm-NAME"}
        version_labels = {}    # version_id -> {label_name: addr} for label: links

        # Map each version's repo source files to the site page that
        # renders them, so prose docs/analyses can keep GitHub-relative
        # file links yet resolve to the right page on the site. Built
        # up-front (over all versions) so an analysis can cite any
        # version's sources.
        source_page_entries = []
        for version_id in source["versions"]:
            version_dirpath = resolve_version_dirpath(repo_dirpath, version_id)
            if version_dirpath is None:
                continue
            meta_filepath = _resolve_version_meta_filepath(version_dirpath)
            rom_meta = json.loads(meta_filepath.read_text()) \
                if meta_filepath.exists() else {}
            source_page_entries.append((version_id, version_dirpath, rom_meta))
        source_page_map = _build_source_page_map(source_page_entries)

        for version_id in source["versions"]:
            version_dirpath = resolve_version_dirpath(repo_dirpath, version_id)
            if version_dirpath is None:
                print(f"  Warning: version directory not found for "
                      f"'{version_id}', skipping")
                continue

            # Get last-modified date from git history
            updated_iso = git_last_modified_iso(repo_dirpath, version_dirpath)
            updated_display = format_display_date(updated_iso) if updated_iso else None

            # Read version metadata (needed to pick the primary listing when
            # a version ships more than one disassembly JSON).
            meta_filepath = _resolve_version_meta_filepath(version_dirpath)
            if meta_filepath.exists():
                rom_meta = json.loads(meta_filepath.read_text())
                title = rom_meta.get("title", f"{name} {version_id}")
                links = list(rom_meta.get("links", []))
            else:
                rom_meta = {}
                title = f"{name} {version_id}"
                links = []

            # Find the primary disassembly JSON (the main program image),
            # excluding any additional listings declared in the metadata.
            data_filepath = _primary_disassembly_json(version_dirpath, rom_meta)
            if data_filepath is None:
                print(f"  Warning: no JSON found in "
                      f"{version_dirpath / 'output'}, skipping")
                continue
            data = json.loads(data_filepath.read_text())

            # Collect valid anchor addresses for this version
            anchors = set()
            for item in data["items"]:
                anchors.add(item["addr"])
                if "binary_addr" in item:
                    anchors.add(item["binary_addr"])
            version_anchors[version_id] = sorted(anchors)

            # Collect memory-map links for this version: addresses with
            # memory_map metadata resolve to the per-version memory-map
            # page, not the listing's ROM-anchor space. Used by the doc
            # and analysis renderers so non-ROM addresses (HAZEL, ZP,
            # MMIO, etc.) cited from prose hit the right page.
            version_mm_links[version_id] = {
                entry["addr"]: f"{version_id}-memory-map.html#mm-{entry['name']}"
                for entry in _normalized_memory_map(data)
            }

            # Label -> address map for `label:NAME` links cited from docs.
            version_labels[version_id] = build_label_addrs(data)

            # Prepend the disassembly-source link. A version may ship the
            # same disassembly in more than one assembler flavour (e.g.
            # beebasm + 64tass); `rom.json` lists them under `sources` and
            # they render as one line with each flavour linked.
            source_link = _build_source_link(rom_meta, repo_url)
            if source_link:
                # Drop any legacy single github link so the source isn't
                # listed twice.
                links = [l for l in links if l.get("icon") != "github"]
                links.insert(0, source_link)

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
                "category": "feedback",
            }

            # Append the memory-map link (our artefact) before the doc
            # pages so the "This disassembly" group reads source →
            # memory map → companion pages. The page is rendered later but
            # its presence is determined by whether the disassembly JSON
            # carries memory_map entries. target="memory-map" pairs the
            # page with the named window so the side-by-side memory-map /
            # listing pattern works; class="mm-link" hooks the link into
            # the listing-page JavaScript that focuses an already-open
            # memory-map tab, matching inline address: links.
            if data.get("memory_map") or data.get("index_bases"):
                links.append({
                    "label": "Memory map",
                    "url": f"{version_id}-memory-map.html",
                    "icon": "map",
                    "target": "memory-map",
                    "class": "mm-link",
                    "category": "ours",
                })

            # Link to the version's additional disassembly listings (e.g.
            # driver variants) — these are peers of this disassembly.
            for extra in rom_meta.get("disassemblies", []):
                links.append({
                    "label": extra["label"],
                    "url": f"{version_id}-{extra['slug']}.html",
                    "icon": "code",
                    "category": "ours",
                })

            # Append our generated companion doc pages (also "This
            # disassembly"). An author can override the inferred category
            # per doc in rom.json.
            for doc in rom_meta.get("docs", []):
                links.append({
                    "label": doc["label"],
                    "url": _doc_output_filename(version_id, doc["path"]),
                    "icon": "doc",
                    "category": doc.get("category", "ours"),
                })

            # Append companion source-view pages. A multi-source artefact
            # is more than its primary disassembly: it may carry extra
            # source files — detokenised BBC BASIC, or machine-code driver
            # variants held encoded in the image — each presentable as its
            # own page. They render as plain text for now (see
            # `_render_source_pages`).
            for entry in rom_meta.get("source_files", []):
                links.append({
                    "label": entry["label"],
                    "url": _source_output_filename(version_id, entry),
                    "icon": entry.get("icon", "code"),
                    "category": entry.get("category", "ours"),
                })

            # Merge the research references into the page's links so the
            # reader sees the full attribution — sources consulted,
            # further reading, discussion — on the disassembly page
            # itself, not only the project index. The shared repo-level
            # references are the default; a version's own rom.json
            # `references` specialise or enrich them (see
            # `_merge_references`). Version-specific rom.json links come
            # first so a shared entry (ROM image, discussion thread) keeps
            # its version-specific form when deduped.
            version_references = _merge_references(
                source.get("references", []), rom_meta.get("references", []))
            links = _dedup_links(links + version_references)
            links.append(report_link)

            link_groups = _group_links(links)

            sections = process_disassembly(
                data, version_id=version_id,
                glossary_lookup=glossary_slug_lookup)
            macros = build_macros(data)

            source_addr_label = _source_address_label(meta_filepath, rom_meta)
            html = disassembly_template.render(
                root="../",
                slug=slug,
                name=name,
                version_id=version_id,
                title=title,
                description=description,
                link_groups=link_groups,
                sections=sections,
                macros=macros,
                subroutines=_filter_subroutines(data),
                updated_iso=updated_iso,
                updated_display=updated_display,
                source_addr_label=source_addr_label,
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
                              glossary_lookup, pages,
                              version_mm_links=version_mm_links,
                              version_labels=version_labels,
                              glossary_slug_lookup=glossary_slug_lookup,
                              source_page_map=source_page_map)

            # Build companion source-view pages for this version
            _render_source_pages(env, source, version_id, version_dirpath,
                                 rom_meta, title, output_dirpath, pages)

            # Build additional disassembly pages (e.g. driver variants),
            # each a formatted, anchored listing like the primary one.
            _render_additional_disassemblies(
                env, source, version_id, version_dirpath, rom_meta, title,
                output_dirpath, glossary_slug_lookup=glossary_slug_lookup,
                pages=pages, updated_iso=updated_iso,
                updated_display=updated_display)

            # Build the memory-map page for this version (if the driver
            # enriched any non-ROM labels with memory-map metadata —
            # including indexing bases, which since schema v4 are ordinary
            # `b`-flagged rows folded into the map).
            mm_entries = _normalized_memory_map(data)
            if mm_entries:
                group_titles = rom_meta.get("memory_map_groups", {})
                meta = data.get("meta", {})
                _render_memory_map_page(env, source, version_id, title,
                                        mm_entries, output_dirpath,
                                        version_anchors, pages,
                                        group_titles=group_titles,
                                        rom_load_addr=meta.get("load_addr"),
                                        rom_end_addr=meta.get("end_addr"),
                                        regions=data.get("regions", []),
                                        version_labels=version_labels,
                                        glossary_slug_lookup=glossary_slug_lookup)

        # Build project-level analysis pages (after all versions, so
        # analyses can link into any version's anchors)
        _render_analysis_pages(env, source, output_dirpath,
                               version_anchors, glossary_lookup, pages,
                               version_mm_links=version_mm_links,
                               version_labels=version_labels,
                               glossary_slug_lookup=glossary_slug_lookup,
                               source_page_map=source_page_map)


def _resolve_version_meta_filepath(version_dirpath):
    """Locate a version's metadata JSON.

    Projects that disassemble ROM images keep it at `rom/rom.json`;
    projects that disassemble ordinary program binaries use the neutral
    `binary/binary.json` layout (the file is a program, not a ROM). Prefer
    the neutral layout, falling back to the ROM layout so existing
    projects are unaffected.
    """
    binary_meta_filepath = version_dirpath / "binary" / "binary.json"
    if binary_meta_filepath.exists():
        return binary_meta_filepath
    return version_dirpath / "rom" / "rom.json"


def _doc_output_filename(version_id, doc_path):
    """Derive the output HTML filename for a doc entry."""
    stem = Path(doc_path).stem.lower()
    return f"{version_id}-{stem}.html"


def _source_output_filename(version_id, source_entry):
    """Derive the output HTML filename for a companion source-view page.

    An entry may supply a short `slug` for a tidy URL
    (`joystik-driver-a.html`); otherwise the source file's stem is used.
    """
    slug = source_entry.get("slug") or Path(source_entry["path"]).stem.lower()
    return f"{version_id}-{slug}.html"


def _source_address_label(meta_filepath, rom_meta):
    """Header for the pre-relocation (stored) address column, shown for a
    listing whose code carries a load-time move.

    ROM disassemblies show that address as `ROM`. Ordinary program
    binaries are loaded at a DFS load address, so the neutral
    `binary/binary.json` layout defaults the header to `Load` — pairing
    with `Exec`, as in Acorn's Load/Exec file metadata. Either default
    can be overridden with a `source_address_label` field in the version
    metadata.
    """
    explicit = rom_meta.get("source_address_label")
    if explicit:
        return explicit
    return "Load" if meta_filepath.name == "binary.json" else "ROM"


def _additional_disassembly_json_names(rom_meta):
    """Basenames of the JSON listings declared as additional disassemblies."""
    return {Path(d["json"]).name for d in rom_meta.get("disassemblies", [])}


def _primary_disassembly_json(version_dirpath, rom_meta):
    """The version's primary disassembly JSON in its `output/` directory.

    A version may ship extra disassembly listings — e.g. JOYSTIK's two
    driver variants, disassembled separately from the main image. Those
    are declared under `disassemblies` in the metadata and excluded here,
    so the primary (the main program image) is chosen deterministically
    rather than by glob order. Prefers the JSON whose stem matches the
    version directory name; falls back to the first remaining JSON.
    """
    json_filepaths = sorted((version_dirpath / "output").glob("*.json"))
    if not json_filepaths:
        return None
    additional = _additional_disassembly_json_names(rom_meta)
    candidates = [p for p in json_filepaths if p.name not in additional]
    if not candidates:
        candidates = json_filepaths
    for candidate in candidates:
        if candidate.stem == version_dirpath.name:
            return candidate
    return candidates[0]


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
                            source_label="", link_class=None, target=None,
                            version_mm_links=None):
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

    Resolution priority for each address:

    1. If a memory-map entry exists for the address (provided via
       `version_mm_links`), the link points to the per-version
       memory-map page with `class="mm-link" target="memory-map"` so
       it pairs with the named-window mechanism inline `address:`
       links use in the listing.
    2. Otherwise, if the address is a ROM-range anchor (provided via
       `version_anchors`), the link points to the listing page at
       `{version}.html#addr-XXXX` (or the nearest preceding anchor
       inside the range).
    3. Otherwise a warning is printed and the original `<a>` tag is
       left in place.

    Flags (after `?`) supported:

    - `hex` — append ` (&<HEX>)` as a second hyperlink to the same
      anchor, with the hex formatted in `<code>` for visual parity
      with backticked labels. The space between the two links and the
      enclosing parentheses are deliberately outside the `<a>` tags so
      only the label and the hex itself are clickable.

    Arguments:

    - `version_anchors` is the per-version sorted-anchor dict built
      during the disassembly-rendering pass.
    - `version_mm_links` is `{version_id: {addr: href}}` for memory-
      map entries, also built during the disassembly-rendering pass.
      Optional; if omitted only ROM-anchor resolution is attempted.
    - `default_version` supplies the version for unqualified URIs
      (omit `@version`). Pass the doc's own `version_id` inside
      per-version docs; pass `None` inside project-level analyses.
    - `source_label` appears in warning messages so authors can find
      the offending source.
    - `link_class` and `target` are injected as attributes on every
      emitted `<a>` for ROM-range links. Memory-map-link attributes
      are fixed (`class="mm-link"`, `target="memory-map"`).
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

        # Priority 1: memory-map entry → memory-map page link.
        mm_for_version = (version_mm_links or {}).get(version, {})
        if addr in mm_for_version:
            url = mm_for_version[addr]
            mm_attrs = ' class="mm-link" target="memory-map"'
            if not flag:
                return f'<a{mm_attrs} href="{url}">{label}</a>'
            if flag.lower() == "hex":
                hex_display = f'<code>&amp;{hex_str.upper()}</code>'
                return (f'<a{mm_attrs} href="{url}">{label}</a> '
                        f'(<a{mm_attrs} href="{url}">{hex_display}</a>)')
            print(f"  Warning: address:{hex_str}@{version} — unknown flag "
                  f"'?{flag}'{src}")
            return match.group(0)

        # Priority 2: ROM-range anchor.
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


_LABEL_URI_RE = re.compile(
    r'<a href="label:'
    r'(?P<name>[^"@?]+)'      # label name
    r'(?P<at>@[^"?]+)?'       # optional @version (preserved)
    r'(?P<flag>\?[^"]*)?'     # optional ?flag (preserved)
    r'">',
)


def apply_label_uri_links(html, version_labels, default_version=None,
                          source_label=""):
    """Translate `label:NAME[@version][?flag]` hrefs into the equivalent
    `address:HEX[@version][?flag]` href, so the downstream
    `apply_address_uri_links` resolves them like any address link. The
    label text is untouched.

    `version_labels` is `{version_id: {name: addr}}`. An unqualified
    `label:` uses `default_version`.
    """
    def rewrite(match):
        name = match.group("name")
        at = match.group("at") or ""
        flag = match.group("flag") or ""
        version = at[1:] if at else default_version
        addr = (version_labels or {}).get(version, {}).get(name)
        if addr is None:
            src = f" ({source_label})" if source_label else ""
            ver = f"@{version}" if version else ""
            print(f"  Warning: label:{name}{ver} — unknown label{src}")
            return match.group(0)
        return f'<a href="address:{addr:04X}{at}{flag}">'

    return _LABEL_URI_RE.sub(rewrite, html)


_GLOSSARY_URI_RE = re.compile(
    r'<a href="glossary:'
    r'(?P<slug>[^"]+)'
    r'">'
    r'(?P<text>.*?)'
    r'</a>',
    re.DOTALL,
)


def apply_glossary_uri_links(html, glossary_slug_lookup, source_label=""):
    """Rewrite `<a href="glossary:SLUG">text</a>` to the glossary-ref
    anchor. `SLUG` is matched case-insensitively against the anchor slug
    (the key of `glossary_slug_lookup`)."""
    def rewrite(match):
        slug = match.group("slug")
        text = match.group("text")
        entry = (glossary_slug_lookup or {}).get(slug.lower())
        if entry is None:
            src = f" ({source_label})" if source_label else ""
            print(f"  Warning: glossary:{slug} — unknown glossary slug{src}")
            return match.group(0)
        return (f'<a href="glossary.html#term-{entry["slug"]}"'
                f' class="glossary-ref"'
                f' data-tip="{escape(entry["tooltip"])}">{text}</a>')

    return _GLOSSARY_URI_RE.sub(rewrite, html)


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
                      glossary_lookup=None, pages=None,
                      version_mm_links=None, version_labels=None,
                      glossary_slug_lookup=None, source_page_map=None):
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

        doc_filename = _doc_output_filename(version_id, doc["path"])
        src_label = f"{source['slug']}/{doc_filename}"

        # Inline [text](glossary:SLUG) references.
        content_html = apply_glossary_uri_links(
            content_html, glossary_slug_lookup, source_label=src_label)

        # Inline [text](label:NAME[@version]) references translate to the
        # equivalent address: URI, resolved below. Unqualified label: /
        # address: URIs default to this doc's own version.
        content_html = apply_label_uri_links(
            content_html, version_labels, default_version=version_id,
            source_label=src_label)

        # Rewrite inline [label](address:HEX[@version]) URIs.
        content_html = apply_address_uri_links(
            content_html, version_anchors,
            default_version=version_id,
            source_label=src_label,
            version_mm_links=version_mm_links)

        # Repoint GitHub-relative links to repo source files at their
        # rendered site pages.
        content_html = _rewrite_source_file_links(
            content_html, md_filepath.parent, source_page_map)

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


def _build_source_page_map(version_entries):
    """Map a repo source file to the site page that renders it.

    Prose docs and analyses cite the repository's own files with
    relative links (`../../versions/…/basic/x.bas`) so they read
    correctly on GitHub. On the site those files aren't served, but each
    now has a rendered page: `source_files` entries have their own
    plain-text page, and the primary disassembly artefacts (the main
    `.asm`/`.json` and the program binary) are shown by the version's
    disassembly page. This returns `{resolved_abs_path: site_filename}`
    so `_rewrite_source_file_links` can repoint those links without the
    Markdown needing site-specific URLs.

    `version_entries` is a list of `(version_id, version_dirpath,
    rom_meta)`.
    """
    page_map = {}
    for version_id, version_dirpath, rom_meta in version_entries:
        # Companion source-view pages win over the primary-disassembly
        # aliases below, so add them first and never overwrite them.
        for entry in rom_meta.get("source_files", []):
            src_filepath = (version_dirpath / entry["path"]).resolve()
            page_map[src_filepath] = _source_output_filename(version_id, entry)

        # Additional disassembly listings (driver variants) have their own
        # formatted page; map their JSON and its sibling assembler output
        # there rather than to the primary disassembly. These also win over
        # the generic output→primary aliasing below.
        for entry in rom_meta.get("disassemblies", []):
            extra_filename = f"{version_id}-{entry['slug']}.html"
            json_filepath = (version_dirpath / entry["json"]).resolve()
            page_map[json_filepath] = extra_filename
            for suffix in (".asm", ".s"):
                page_map[json_filepath.with_suffix(suffix)] = extra_filename

        disassembly_filename = f"{version_id}.html"
        # The main assembler/JSON listings and the program binary all
        # belong to the primary disassembly page.
        alias_filepaths = []
        output_dirpath = version_dirpath / "output"
        if output_dirpath.is_dir():
            alias_filepaths += [
                p for p in output_dirpath.iterdir()
                if p.suffix in (".asm", ".json", ".s")
            ]
        binary_dirpath = version_dirpath / "binary"
        if binary_dirpath.is_dir():
            alias_filepaths += [
                p for p in binary_dirpath.iterdir()
                if p.name != "binary.json"
            ]
        for alias_filepath in alias_filepaths:
            resolved = alias_filepath.resolve()
            page_map.setdefault(resolved, disassembly_filename)
    return page_map


def _rewrite_source_file_links(html, doc_dirpath, source_page_map):
    """Repoint links that target a repository source file at its page.

    `doc_dirpath` is the on-disk directory the Markdown lived in, so a
    relative href resolves the same way a reader on GitHub would resolve
    it. Only links whose resolved target is a known source file are
    rewritten; everything else (external URLs, in-page anchors, links to
    files with no page) is left untouched.
    """
    if not source_page_map:
        return html

    def repl(match):
        href = match.group(1)
        fragment = match.group(2) or ""
        target = (doc_dirpath / href).resolve()
        site_filename = source_page_map.get(target)
        if site_filename:
            return f'href="{site_filename}{fragment}"'
        return match.group(0)

    # Relative hrefs only (no scheme, no leading //, no in-page anchor).
    return re.sub(
        r'href="((?!https?:|//|#)[^"#?]+)(#[^"]*)?"', repl, html)


def _render_additional_disassemblies(env, source, version_id, version_dirpath,
                                     rom_meta, parent_title, output_dirpath,
                                     glossary_slug_lookup=None, pages=None,
                                     updated_iso=None, updated_display=None):
    """Render each additional disassembly listing declared for a version.

    A program can comprise more than one separately-disassembled body of
    code — JOYSTIK, for instance, carries two 256-byte driver variants
    disassembled apart from its main image. Each is declared under
    `disassemblies` in the version metadata (label, slug, json) and gets
    its own formatted, anchored listing page (and a memory-map page when
    the listing carries one), exactly like the primary disassembly — so a
    driver variant reads as a first-class disassembly, not plain text.

    Navigation links each variant back to the parent program and across
    to its sibling variants.
    """
    disassemblies = rom_meta.get("disassemblies", [])
    if not disassemblies:
        return

    disassembly_template = env.get_template("_disassembly.html")
    slug = source["slug"]
    name = source["name"]
    description = source["description"]
    source_addr_label = _source_address_label(
        _resolve_version_meta_filepath(version_dirpath), rom_meta)

    for entry in disassemblies:
        json_filepath = version_dirpath / entry["json"]
        if not json_filepath.exists():
            print(f"  Warning: disassembly JSON {json_filepath} not found, "
                  f"skipping")
            continue
        data = json.loads(json_filepath.read_text())
        page_id = f"{version_id}-{entry['slug']}"

        # Navigation: back to the parent program, then across to siblings,
        # then this listing's own memory map (if any).
        links = [{
            "label": f"{parent_title} disassembly",
            "url": f"{version_id}.html",
            "icon": "code",
            "category": "ours",
        }]
        for sibling in disassemblies:
            if sibling["slug"] == entry["slug"]:
                continue
            links.append({
                "label": sibling["label"],
                "url": f"{version_id}-{sibling['slug']}.html",
                "icon": "code",
                "category": "ours",
            })
        if data.get("memory_map") or data.get("index_bases"):
            links.append({
                "label": "Memory map",
                "url": f"{page_id}-memory-map.html",
                "icon": "map",
                "target": "memory-map",
                "class": "mm-link",
                "category": "ours",
            })
        link_groups = _group_links(links)

        sections = process_disassembly(
            data, version_id=page_id, glossary_lookup=glossary_slug_lookup)
        macros = build_macros(data)

        html = disassembly_template.render(
            root="../",
            slug=slug,
            name=name,
            version_id=page_id,
            title=entry["label"],
            description=entry.get("runtime", description),
            link_groups=link_groups,
            sections=sections,
            macros=macros,
            subroutines=_filter_subroutines(data),
            updated_iso=updated_iso,
            updated_display=updated_display,
            source_addr_label=source_addr_label,
        )
        (output_dirpath / f"{page_id}.html").write_text(html)
        print(f"  {slug}/{page_id}.html")
        if pages is not None:
            pages.append({
                "url": f"{BASE_URL}{slug}/{page_id}.html",
                "title": entry["label"],
                "description": description,
                "is_disassembly": True,
                "updated": updated_iso,
            })

        # A memory-map page too, if this listing enriched any non-ROM
        # labels with memory-map metadata.
        mm_entries = _normalized_memory_map(data)
        if mm_entries:
            anchors = set()
            for item in data["items"]:
                anchors.add(item["addr"])
                if "binary_addr" in item:
                    anchors.add(item["binary_addr"])
            meta = data.get("meta", {})
            _render_memory_map_page(
                env, source, page_id, entry["label"], mm_entries,
                output_dirpath, {page_id: sorted(anchors)}, pages,
                group_titles=rom_meta.get("memory_map_groups", {}),
                rom_load_addr=meta.get("load_addr"),
                rom_end_addr=meta.get("end_addr"),
                regions=data.get("regions", []),
                version_labels={page_id: build_label_addrs(data)},
                glossary_slug_lookup=glossary_slug_lookup)


def _render_source_pages(env, source, version_id, version_dirpath, rom_meta,
                         disassembly_title, output_dirpath, pages=None):
    """Build plain-text source-view pages for a version.

    A multi-source artefact is more than its primary disassembly. A
    version may list companion source files under `source_files` in its
    metadata — for example a detokenised BBC BASIC front-end carried as
    editable text, or machine-code driver variants that live encoded in
    the image and are assembled back at build time. Each is rendered
    verbatim inside a <pre> block so every source that makes up the
    artefact is presentable and linkable.

    This is deliberately plain: no syntax highlighting or cross-linking
    yet. It reuses the doc page shell (heading, back link, base layout)
    so a source view sits naturally beside the disassembly and its docs.
    """
    source_files = rom_meta.get("source_files", [])
    if not source_files:
        return

    doc_template = env.get_template("_doc.html")
    slug = source["slug"]

    for entry in source_files:
        src_filepath = version_dirpath / entry["path"]
        if not src_filepath.exists():
            print(f"  Warning: source file {src_filepath} not found, skipping")
            continue

        text = src_filepath.read_text()
        language = entry.get("language", "")
        origin_filename = Path(entry["path"]).name
        meta_bits = " · ".join(bit for bit in (language, origin_filename) if bit)

        caption_html = (
            f'<p class="source-meta">{escape(meta_bits)}</p>' if meta_bits else "")
        listing_html = f'<pre class="source-listing">{escape(text)}</pre>'
        content_html = caption_html + listing_html

        source_filename = _source_output_filename(version_id, entry)
        html = doc_template.render(
            root="../",
            slug=slug,
            doc_filename=source_filename,
            version_id=version_id,
            title=entry["label"],
            description=source["description"],
            content=Markup(content_html),
            back_url=f"{version_id}.html",
            back_label=f"{disassembly_title} disassembly",
        )

        output_filepath = output_dirpath / source_filename
        output_filepath.write_text(html)
        print(f"  {slug}/{source_filename}")
        if pages is not None:
            pages.append({
                "url": f"{BASE_URL}{slug}/{source_filename}",
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
                           pages=None, version_mm_links=None,
                           version_labels=None, glossary_slug_lookup=None,
                           source_page_map=None):
    """Build project-level analysis pages from acornaeology.json.

    Each entry in `source["analyses"]` points to a Markdown file
    inside the source repo and is rendered to a standalone HTML
    page at `<slug>/<stem>.html`. Inline `address:` / `label:` /
    `glossary:` links behave as for per-version docs (analyses are
    project-level, so `address:` / `label:` must carry `@version`).
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

        analysis_filename = _analysis_output_filename(analysis["url"])
        src_label = f"{slug}/{analysis_filename}"

        # Inline [text](glossary:SLUG) references.
        content_html = apply_glossary_uri_links(
            content_html, glossary_slug_lookup, source_label=src_label)

        # Inline [text](label:NAME@version) references. Analyses are
        # project-level, so label: (like address:) must carry @version.
        content_html = apply_label_uri_links(
            content_html, version_labels, default_version=None,
            source_label=src_label)

        # Rewrite inline [label](address:HEX[@version]) URIs. Analyses
        # are project-level, so there's no implicit "current version";
        # authors must always specify @version. apply_address_uri_links
        # warns on unqualified references rather than guessing.
        content_html = apply_address_uri_links(
            content_html, version_anchors,
            default_version=None,
            source_label=src_label,
            version_mm_links=version_mm_links)

        # Repoint GitHub-relative links to repo source files at their
        # rendered site pages.
        content_html = _rewrite_source_file_links(
            content_html, md_filepath.parent, source_page_map)

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




# Pre-v4 documents carried `access` as a mutually-exclusive scalar
# string; map each to its v4 orthogonal-flag list.
_V3_SCALAR_ACCESS = {"r": ["r"], "w": ["w"], "rw": ["r", "w"]}


def _normalized_memory_map(data):
    """Return this document's memory-map entries in the v4 shape.

    v4 (`meta.schema_version >= 4`) models memory access as an
    orthogonal ``["r", "w", "b"]`` subset per row and folds indexing
    bases into `memory_map` as `b`-flagged rows — that list is returned
    as-is.

    v2/v3 carried `access` as a mutually-exclusive scalar (`r`/`w`/`rw`)
    and split indexing bases into a separate top-level `index_bases`
    array (no `access` field — a base is base-only). Such documents are
    normalised to the v4 shape: the scalar becomes a list and each index
    base is appended as an `["b"]` row. So a single inline rendering path
    serves every schema version — sibling repos upgrade dasmos on
    independent schedules, so v2/v3 and v4 documents coexist.
    """
    schema_version = data.get("meta", {}).get("schema_version", 2)
    memory_map = data.get("memory_map", [])
    if schema_version >= 4:
        return list(memory_map)
    normalized = []
    for entry in memory_map:
        entry = dict(entry)
        access = entry.get("access")
        if isinstance(access, str):
            entry["access"] = _V3_SCALAR_ACCESS.get(
                access.lower(), [access.lower()])
        normalized.append(entry)
    for base in data.get("index_bases", []):
        base = dict(base)
        base["access"] = ["b"]
        normalized.append(base)
    return normalized


# Single-letter access-flag labels for the memory-map Access column.
_ACCESS_LABELS = {"r": "R", "w": "W", "b": "B"}


def _render_memory_map_page(env, source, version_id, version_title,
                            memory_map, output_dirpath, version_anchors,
                            pages=None, group_titles=None,
                            rom_load_addr=None, rom_end_addr=None,
                            regions=None, version_labels=None,
                            glossary_slug_lookup=None):
    """Render {version_id}-memory-map.html for one version of a project.

    `memory_map` is this version's entries already normalised to the v4
    shape by `_normalized_memory_map` (so v2/v3 and v4 documents render
    identically). Each entry is:

        {addr, name, [length, group, access, description]}

    `access` is an orthogonal flag list — a subset of `["r", "w", "b"]`
    (read / write / indexing base). A `["b"]`-only row is an indexing
    base: named as `base,X`/`,Y` but with the literal byte never touched,
    so it sits *in place* within its group (not off in a separate
    section), the `B` flag alone marking that the ROM doesn't own it. An
    address that is read/written *and* indexed-through comes out e.g.
    `["r", "w", "b"]`.

    The memory map is version-scoped because workspace layout can shift
    between ROM releases. Descriptions pass through the standard
    Markdown pipeline; `[label](address:HEX)` links resolve to either
    `{version_id}.html#addr-XXXX` (ROM code) or `#mm-NAME` (other
    entries on the same memory-map page).

    `group_titles` maps memory-map group keys (e.g. `zero_page`,
    `hazel`) to display titles. Sourced directly from this version's
    `rom.json` `memory_map_groups` field. Unmapped groups fall back
    to the title-cased key.

    `regions` (Layer B) declare an anchor label plus an
    offset window; the anchor's memory-map row is annotated with its
    span so the reader sees which `anchor±k` slots it groups.
    """
    if group_titles is None:
        group_titles = {}
    regions = regions or []
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
        # glossary: → anchor; label: → equivalent address: URI, so it then
        # flows through the same #mm-NAME / ROM-anchor resolution below.
        html = apply_glossary_uri_links(
            html, glossary_slug_lookup,
            source_label=f"{source['slug']}/{output_filename}")
        html = apply_label_uri_links(
            html, version_labels, default_version=version_id,
            source_label=f"{source['slug']}/{output_filename}")
        html = rewrite_mm_refs(html)
        html = apply_address_uri_links(
            html, version_anchors,
            default_version=version_id,
            source_label=f"{source['slug']}/{output_filename}",
            link_class="listing-link",
            target="listing")
        return Markup(html)

    def access_display(flags):
        if not flags:
            return ""
        return "/".join(_ACCESS_LABELS.get(f, f.upper()) for f in flags)

    def addr_display(entry):
        start = entry["addr"]
        length = entry.get("length") or 1
        if length <= 1:
            return f"&{start:04X}"
        return f"&{start:04X}–&{start + length - 1:04X}"

    # A region anchor's memory-map row is annotated with the address span
    # its window groups, so the reader sees which `anchor±k` slots the
    # anchor label spans. `window` is an inclusive offset range [lo, hi].
    region_by_anchor = {r["anchor"]: r for r in regions}

    def region_span_display(addr):
        region = region_by_anchor.get(addr)
        if not region:
            return None
        lo, hi = region["window"]
        return f"&{addr + lo:04X}–&{addr + hi:04X}"

    groups = []
    for g in group_order:
        entries = [
            {
                "addr_display": addr_display(e),
                "name": e["name"],
                "access_display": access_display(e.get("access")),
                "description_html": render_description(e.get("description")),
                "region_span": region_span_display(e["addr"]),
            }
            # Sort within a group by address so a base folds into its
            # place in the layout (v4 already emits address-sorted; a
            # normalised v2/v3 document appends its former index bases
            # after the owned rows, so sort to reunite them).
            for e in sorted(group_entries[g], key=lambda e: e["addr"])
        ]
        groups.append({
            "name": group_titles.get(g, g.replace("_", " ").title()),
            "slug": g.replace("_", "-"),
            "entries": entries,
        })

    # ROM code range for the intro pointer to the listing. Sourced from
    # the disassembly's own `meta` (load_addr / end_addr, the latter
    # exclusive) so it's correct for any ROM placement — no hard-coded
    # address. Falls back to None if an older JSON lacks the metadata.
    rom_start_hex = f"{rom_load_addr:04X}" if rom_load_addr is not None else None
    rom_end_hex = (
        f"{rom_end_addr - 1:04X}"
        if rom_end_addr is not None else None
    )

    template = env.get_template("_memory_map.html")
    html = template.render(
        root="../",
        slug=source["slug"],
        name=source["name"],
        version_id=version_id,
        title=version_title,
        output_filename=output_filename,
        groups=groups,
        rom_start_hex=rom_start_hex,
        rom_end_hex=rom_end_hex,
    )
    (output_dirpath / output_filename).write_text(html)
    print(f"  {source['slug']}/{output_filename}")
    if pages is not None:
        pages.append({"url": f"{BASE_URL}{source['slug']}/{output_filename}"})


def _filter_subroutines(data):
    """Return ROM-range subroutine and banner entries, in address order.

    The version-page sidebar TOC lists everything that has its own
    ``<div class="sub-header">`` block in the listing — so both real
    subroutines (``data["subroutines"]``) and standalone banners
    (``data["banners"]`` — dasmos's analogue of py8dis's
    ``data_banner``). Both share the same shape (``addr``, ``name``,
    ``title``, ``description``) and the same TOC styling, so they
    merge cleanly here.
    """
    meta = data.get("meta", {})
    load_addr = meta.get("load_addr", 0)
    end_addr = meta.get("end_addr", 0xFFFF)
    combined = list(data.get("subroutines", [])) + list(data.get("banners", []))
    in_range = [s for s in combined if load_addr <= s["addr"] < end_addr]
    in_range.sort(key=lambda s: s["addr"])
    return in_range


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
