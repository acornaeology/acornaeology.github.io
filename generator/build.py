#!/usr/bin/env python3
"""Static site builder for acornaeology.uk"""

import argparse
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

from dataclasses import dataclass, field
from datetime import datetime

from .anchors import AnchorIndex, anchor_id, parse_link_flags
from .disassembly import build_label_addrs, build_macros, process_disassembly
from .feed import generate_atom_feed, generate_sitemap
from .glossary import build_glossary_lookup, parse_glossary
from .headings import add_heading_ids
from .listings import (
    AddressMiss,
    ListingLinks,
    listing_key,
    listing_page_stem,
    resolve_listing_key,
)


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
    ("download", "Downloads"),
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
    "disc": "download",
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


def resolve_source(source, base_dirpath=DATA_DIRPATH):
    """Resolve a disassembly source to a local directory path.

    Uses the local path if available, otherwise clones the repo. A
    relative `path` is resolved against `base_dirpath`, the directory
    of the sources file that lists it.
    """
    if "path" in source:
        local_dirpath = (base_dirpath / source["path"]).resolve()
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


DEFAULT_SOURCES_FILEPATH = DATA_DIRPATH / "sources.json"


def load_sources(sources_filepath=DEFAULT_SOURCES_FILEPATH):
    """Load and resolve all disassembly sources.

    Returns a list of dicts with manifest metadata and resolved repo path.
    """
    if not sources_filepath.exists():
        return []

    sources = json.loads(sources_filepath.read_text())
    result = []

    for source in sources:
        repo_dirpath = resolve_source(
            source, base_dirpath=sources_filepath.parent)
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


def _localise_reference_assets(references, repo_dirpath, slug, output_dirpath):
    """Serve repo-local reference files from the site and rewrite their URLs.

    A reference whose ``url`` is a repo-relative path to a file in the source
    repo (e.g. ``docs/sn74ls157.pdf`` or ``disc/foo.ssd``) would otherwise be
    a dead link: the generator copies no source-repo files. Copy any such file
    to ``<output>/<slug>/<url>`` and rewrite the reference ``url`` to the
    absolute site URL, so it resolves identically from the index and every
    version page. URLs that are external (scheme-qualified), site-absolute, a
    fragment/mailto, or that don't resolve to a real file inside the repo are
    left untouched.
    """
    repo_root = repo_dirpath.resolve()
    for ref in references:
        url = ref.get("url", "")
        if not url or "://" in url or url.startswith(("/", "#", "mailto:")):
            continue
        src_filepath = (repo_dirpath / url).resolve()
        if not src_filepath.is_file():
            continue
        # Never copy from outside the source repo.
        if not src_filepath.is_relative_to(repo_root):
            continue
        dst_filepath = output_dirpath / url
        dst_filepath.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_filepath, dst_filepath)
        ref["url"] = f"{BASE_URL}{slug}/{Path(url).as_posix()}"


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

        # Copy repo-local reference files (PDFs, disc images, …) into the
        # output and rewrite their URLs to the served copies.
        _localise_reference_assets(
            source["references"], repo_dirpath, slug, output_dirpath)

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

        # Pass 1: load every version and its listings, and index each
        # listing's anchors, labels and memory-map links, so that any
        # page — a listing, a doc, a memory map or an analysis — can
        # link into any listing of the project.
        plans = [plan for plan in (
            _load_version(repo_dirpath, slug, name, version_id)
            for version_id in source["versions"]) if plan is not None]
        links = ListingLinks()
        for plan in plans:
            for listing in plan.listings:
                _register_listing(links, listing, slug)

        # Build version metadata for the index page. The rom.json docs
        # array has a `"type": "changes"` marker distinguishing the
        # changes-from doc from any other per-version docs.
        versions = [_index_version_entry(plan) for plan in plans]

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

        # Map each version's repo source files to the site page that
        # renders them, so prose docs/analyses can keep GitHub-relative
        # file links yet resolve to the right page on the site. Built
        # up-front (over all versions) so an analysis can cite any
        # version's sources.
        source_page_map = _build_source_page_map(
            [(plan.version_id, plan.version_dirpath, plan.rom_meta)
             for plan in plans])

        # Pass 2: render each version's pages.
        for plan in plans:
            version_id = plan.version_id
            version_dirpath = plan.version_dirpath
            rom_meta = plan.rom_meta
            title = plan.title
            links_list = list(rom_meta.get("links", []))

            # Get last-modified date from git history
            updated_iso = git_last_modified_iso(repo_dirpath, version_dirpath)
            updated_display = format_display_date(updated_iso) if updated_iso else None

            primary = plan.primary
            if primary is None:
                print(f"  Warning: no JSON found in "
                      f"{version_dirpath / 'output'}, skipping")
                continue
            data = primary.data
            anchors = primary.anchors

            # Prepend the disassembly-source link. A version may ship the
            # same disassembly in more than one assembler flavour (e.g.
            # beebasm + 64tass); `rom.json` lists them under `sources` and
            # they render as one line with each flavour linked.
            source_link = _build_source_link(rom_meta, repo_url)
            if source_link:
                # Drop any legacy single github link so the source isn't
                # listed twice.
                links_list = [l for l in links_list
                              if l.get("icon") != "github"]
                links_list.insert(0, source_link)

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
            if primary.has_memory_map:
                links_list.append(_memory_map_link(primary))

            # Link to the version's other listings: driver variants, or
            # the other programs on a disc.
            for extra in plan.extras:
                links_list.append(_listing_link(extra))

            # Append our generated companion doc pages (also "This
            # disassembly"). An author can override the inferred category
            # per doc in rom.json.
            for doc in rom_meta.get("docs", []):
                links_list.append({
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
                links_list.append({
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
            links_list = _dedup_links(links_list + version_references)
            links_list.append(report_link)

            link_groups = _group_links(links_list)

            sections = process_disassembly(
                data, version_id=version_id,
                glossary_lookup=glossary_slug_lookup, anchors=anchors,
                links=links, listing_key=primary.key)
            macros = build_macros(data)

            source_addr_label = _source_address_label(
                plan.meta_filepath, rom_meta)
            html = disassembly_template.render(
                root="../",
                slug=slug,
                name=name,
                version_id=version_id,
                title=primary.label if plan.peers else title,
                context=title if plan.peers else None,
                description=description,
                link_groups=link_groups,
                sections=sections,
                macros=macros,
                subroutines=_filter_subroutines(data, anchors),
                updated_iso=updated_iso,
                updated_display=updated_display,
                source_addr_label=source_addr_label,
                files=_file_rows(primary.files),
            )

            version_filepath = output_dirpath / f"{version_id}.html"
            version_filepath.write_text(html)
            print(f"  {slug}/{version_id}.html")
            pages.append({
                "url": f"{BASE_URL}{slug}/{version_id}.html",
                "title": (f"{primary.label}, {title}" if plan.peers
                          else title),
                "description": description,
                "is_disassembly": True,
                "updated": updated_iso,
            })

            # Build doc pages for this version
            _render_doc_pages(env, source, version_id, version_dirpath,
                              rom_meta, output_dirpath, links, pages,
                              glossary_slug_lookup=glossary_slug_lookup,
                              source_page_map=source_page_map)

            # Build companion source-view pages for this version
            _render_source_pages(env, source, version_id, version_dirpath,
                                 rom_meta, title, output_dirpath, pages)

            # Build the version's other listing pages (driver variants,
            # or a disc's other programs), each a formatted, anchored
            # listing like the primary one.
            _render_additional_disassemblies(
                env, source, plan, output_dirpath, links,
                glossary_slug_lookup=glossary_slug_lookup,
                pages=pages, updated_iso=updated_iso,
                updated_display=updated_display)

            # Build the memory-map page for this version (if the driver
            # enriched any non-ROM labels with memory-map metadata —
            # including indexing bases, which since schema v4 are ordinary
            # `b`-flagged rows folded into the map).
            _render_listing_memory_map(env, source, plan, primary,
                                       primary.label if plan.peers else title,
                                       output_dirpath, links, pages,
                                       glossary_slug_lookup)

        # Build project-level analysis pages (after all versions, so
        # analyses can link into any version's anchors)
        _render_analysis_pages(env, source, output_dirpath, links, pages,
                               glossary_slug_lookup=glossary_slug_lookup,
                               source_page_map=source_page_map)


@dataclass
class _Listing:
    """One listing page of a version: its primary listing (`slug` None)
    or one declared under `disassemblies` (`entry` is that declaration)."""
    key: str
    slug: str | None
    label: str
    data: dict
    anchors: AnchorIndex
    entry: dict = field(default_factory=dict)
    files: list = field(default_factory=list)

    @property
    def page_stem(self):
        return listing_page_stem(self.key)

    @property
    def has_memory_map(self):
        return bool(self.data.get("memory_map")
                    or self.data.get("index_bases"))


@dataclass
class _VersionPlan:
    """A version's metadata and listings, loaded before anything renders."""
    version_id: str
    version_dirpath: Path
    meta_filepath: Path
    rom_meta: dict
    title: str
    primary: _Listing | None
    extras: list

    @property
    def listings(self):
        return ([self.primary] if self.primary else []) + self.extras

    @property
    def peers(self):
        """Whether the version's listings are peers — separate programs
        rather than a program and its parts. A version says so by giving
        its primary listing a `label` of its own."""
        return "label" in self.rom_meta


def _load_version(repo_dirpath, slug, name, version_id):
    """Load a version's metadata and listings, or None (with a warning)
    if its directory is missing."""
    version_dirpath = resolve_version_dirpath(repo_dirpath, version_id)
    if version_dirpath is None:
        print(f"  Warning: version directory not found for "
              f"'{version_id}', skipping")
        return None
    meta_filepath = _resolve_version_meta_filepath(version_dirpath)
    rom_meta = (json.loads(meta_filepath.read_text())
                if meta_filepath.exists() else {})
    title = rom_meta.get("title", f"{name} {version_id}")

    primary = None
    primary_json_filepath = _primary_disassembly_json(
        version_dirpath, rom_meta)
    if primary_json_filepath is not None:
        data = json.loads(primary_json_filepath.read_text())
        key = listing_key(version_id)
        primary = _Listing(
            key=key, slug=None, label=rom_meta.get("label", title),
            files=rom_meta.get("files", []), data=data,
            anchors=_listing_anchors(data, f"{slug}/{key}.html"))

    extras = []
    for entry in rom_meta.get("disassemblies", []):
        json_filepath = version_dirpath / entry["json"]
        if not json_filepath.exists():
            print(f"  Warning: disassembly JSON {json_filepath} not found, "
                  f"skipping")
            continue
        data = json.loads(json_filepath.read_text())
        key = listing_key(version_id, entry["slug"])
        extras.append(_Listing(
            key=key, slug=entry["slug"], label=entry["label"], data=data,
            files=entry.get("files", []),
            anchors=_listing_anchors(
                data, f"{slug}/{listing_page_stem(key)}.html"),
            entry=entry))

    return _VersionPlan(version_id, version_dirpath, meta_filepath,
                        rom_meta, title, primary, extras)


def _register_listing(links, listing, slug):
    """Index `listing` in the project's `ListingLinks`, and warn if its
    key is taken (two listings would share a page)."""
    if listing.key in links:
        print(f"  Warning: {slug} declares listing {listing.key} twice")
    links.anchors[listing.key] = listing.anchors
    links.labels[listing.key] = build_label_addrs(listing.data)
    # Addresses with memory-map metadata resolve to the listing's
    # memory-map page, not to its rows, so non-ROM addresses (HAZEL, ZP,
    # MMIO, etc.) cited from prose hit the right page.
    links.mm_links[listing.key] = {
        entry["addr"]: f"{listing.page_stem}-memory-map.html#mm-{entry['name']}"
        for entry in _normalized_memory_map(listing.data)
    }


def _acorn_hex(value):
    """`value` (an int, or a hex string such as `"0x0400"`, `"&0400"` or
    `"0400"`) as Acorn-style hex, `&0400`; a string that isn't hex is
    shown as it is."""
    if value is None or value == "":
        return ""
    if isinstance(value, int):
        return f"&{value:04X}"
    text = str(value).strip()
    digits = text[2:] if text[:2].lower() == "0x" else text.lstrip("&")
    try:
        return f"&{int(digits, 16):04X}"
    except ValueError:
        return text


def _file_rows(files):
    """Rows for a listing's Files table, from the `files` its metadata
    declares (`[{name, load, exec, size, sha256}]`): the program's files
    as they are on the disc or tape. Load, exec and length are shown in
    hex, as `*INFO` shows them."""
    return [
        {
            "name": f.get("name", ""),
            "load": _acorn_hex(f.get("load")),
            "exec": _acorn_hex(f.get("exec")),
            "length": _acorn_hex(f.get("size")),
            "sha256": f.get("sha256", ""),
        }
        for f in files
    ]


def _memory_map_link(listing):
    """The page link to a listing's memory map."""
    return {
        "label": "Memory map",
        "url": f"{listing.page_stem}-memory-map.html",
        "icon": "map",
        "target": "memory-map",
        "class": "mm-link",
        "category": "ours",
    }


def _listing_link(listing, label=None):
    """The page link to another listing of the same version."""
    return {
        "label": label or listing.label,
        "url": f"{listing.page_stem}.html",
        "icon": "code",
        "category": "ours",
    }


def _index_version_entry(plan):
    """A version's row (and sub-rows) on the project index."""
    version_id = plan.version_id
    rom_meta = plan.rom_meta

    changes_doc = None
    other_docs = []
    for doc in rom_meta.get("docs", []):
        if doc.get("type") == "changes":
            changes_doc = doc
        else:
            other_docs.append(doc)

    def listing_row(listing):
        return {
            "label": listing.label,
            "disassembly_url": f"{listing.page_stem}.html",
            "memory_map_url": (
                f"{listing.page_stem}-memory-map.html"
                if listing.has_memory_map else None),
        }

    # A version of peer programs lists every program, the primary
    # included, beneath its title; otherwise the title is the primary
    # listing and the others are its parts.
    primary = plan.primary
    sub_listings = plan.listings if plan.peers else plan.extras
    return {
        "id": version_id,
        "title": plan.title,
        "peers": plan.peers,
        "disassembly_url": f"{version_id}.html",
        "memory_map_url": (
            f"{version_id}-memory-map.html"
            if primary is not None and primary.has_memory_map
            and not plan.peers else None
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
            for sf in rom_meta.get("source_files", [])
        ],
        "additional_disassemblies": [listing_row(l) for l in sub_listings],
    }


def _render_listing_memory_map(env, source, plan, listing, title,
                               output_dirpath, links, pages,
                               glossary_slug_lookup):
    """Render `listing`'s memory-map page, if its JSON carries a memory
    map."""
    mm_entries = _normalized_memory_map(listing.data)
    if not mm_entries:
        return
    meta = listing.data.get("meta", {})
    _render_memory_map_page(
        env, source, listing.key, title, mm_entries, output_dirpath,
        links, pages,
        group_titles=plan.rom_meta.get("memory_map_groups", {}),
        rom_load_addr=meta.get("load_addr"),
        rom_end_addr=meta.get("end_addr"),
        regions=listing.data.get("regions", []),
        glossary_slug_lookup=glossary_slug_lookup)


def _listing_anchors(data, page_label):
    """Index a listing's anchors, warning about any duplicated ids it
    can't qualify (JSON from dasmos before 4.4.0 doesn't name moves)."""
    anchors = AnchorIndex.from_data(data)
    duplicates = sorted(set(anchors.unresolved_duplicates))
    if duplicates:
        print(f"  Warning: {page_label} has {len(duplicates)} duplicated "
              f"anchor ids (e.g. {duplicates[0]}) because copies overlap "
              f"and the JSON doesn't name their moves; regenerate it with "
              f"dasmos >= 4.4.0")
    return anchors


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


_ADDRESS_URI_RE = re.compile(
    r'<a href="address:'
    r'([0-9A-Fa-f]{4,})'     # hex address
    r'(?:@([^"?]*))?'        # optional @listing
    r'(?:\?([^"]*))?'        # optional ?flag1[&flag2...]
    r'">'
    r'(.*?)'                 # label (markdown-converted, may contain HTML)
    r'</a>',
    re.IGNORECASE | re.DOTALL,
)


def apply_address_uri_links(html, links, default_listing=None,
                            source_label="", link_class=None, target=None):
    """Rewrite Markdown-authored `address:HEX[@listing][?flag]` URIs.

    Authors write inline links in their Markdown sources such as

        [rx_frame_b](address:E263)                  — label-only link
        [rx_frame_b](address:E263?hex)              — append " (&E263)"
        [rx_frame_b](address:E263@3.60?hex)         — explicit version
        [entry](address:23B2@disc_master/editor)    — another listing
        [entry](address:23B2@/editor)               — one in this version
        [rx_frame_b (&E263)](address:E263@3.60)     — author wrote label
                                                      fully by hand

    Markdown converts those to `<a href="address:…">label</a>`; this
    post-processor resolves each URI to the matching listing page and
    anchor and rewrites the output. `@listing` is a listing key (see
    `listings`); without one, the link goes to `default_listing`.

    Resolution (`ListingLinks.address_target`): a memory-map entry for
    the address links to the listing's memory-map page, with
    `class="mm-link" target="memory-map"` so it pairs with the
    named-window mechanism inline `address:` links use in the listing.
    Otherwise the link goes to the listing row at or before the address.
    Failing both, a warning is printed and the `<a>` is left in place.

    Flags (after `?`, separated by `&`; see `anchors.parse_link_flags`):

    - `hex` — append ` (&<HEX>)` as a second hyperlink to the same
      anchor, with the hex formatted in `<code>` for visual parity
      with backticked labels. The space between the two links and the
      enclosing parentheses are deliberately outside the `<a>` tags so
      only the label and the hex itself are clickable.
    - `move=NAME`, `unmoved`, `stored` — which copy of the code to link
      to, when moves overlap: the copy stored in move NAME, the copy
      that runs where it is stored, or the row whose stored address
      this is. Without one, a link to an address where several copies
      run goes to the default copy (see `anchors`) with a warning.

    Arguments:

    - `links` is the project's `ListingLinks`.
    - `default_listing` is the listing key for URIs without `@`, and the
      base of `@/SLUG`. Pass a doc's own version inside per-version
      docs; pass `None` inside project-level analyses.
    - `source_label` appears in warning messages so authors can find
      the offending source.
    - `link_class` and `target` are injected as attributes on every
      emitted `<a>` for listing-row links. Memory-map-link attributes
      are fixed (`class="mm-link"`, `target="memory-map"`).
    """

    extra_attrs = ""
    if link_class:
        extra_attrs += f' class="{link_class}"'
    if target:
        extra_attrs += f' target="{target}"'

    def rewrite(match):
        hex_str, at, flag_str, label = match.groups()
        key = resolve_listing_key(at, default_listing)
        src = f" ({source_label})" if source_label else ""
        flags = parse_link_flags(flag_str)
        where = f"address:{hex_str}@{key if key is not None else at}"

        if key is None:
            print(f"  Warning: address:{hex_str}{'@' + at if at else ''} "
                  f"names no listing and no default is available "
                  f"here{src}")
            return match.group(0)

        result = links.address_target(key, int(hex_str, 16), flags)
        if isinstance(result, AddressMiss):
            print(f"  Warning: {where} — {result.reason}{src}")
            return match.group(0)
        if result.warning:
            print(f"  Warning: {where} — {result.warning}{src}")

        attrs = (' class="mm-link" target="memory-map"'
                 if result.memory_map else extra_attrs)
        url = result.href
        if not flags.hex:
            return f'<a{attrs} href="{url}">{label}</a>'
        hex_display = f'<code>&amp;{hex_str.upper()}</code>'
        return (f'<a{attrs} href="{url}">{label}</a> '
                f'(<a{attrs} href="{url}">{hex_display}</a>)')

    return _ADDRESS_URI_RE.sub(rewrite, html)


_LABEL_URI_RE = re.compile(
    r'<a href="label:'
    r'(?P<name>[^"@?]+)'      # label name
    r'(?P<at>@[^"?]*)?'       # optional @listing (preserved)
    r'(?P<flag>\?[^"]*)?'     # optional ?flag (preserved)
    r'">',
)


def apply_label_uri_links(html, links, default_listing=None,
                          source_label=""):
    """Translate `label:NAME[@listing][?flag]` hrefs into the equivalent
    `address:HEX[@listing][?flag]` href, so the downstream
    `apply_address_uri_links` resolves them like any address link. The
    label text is untouched.

    `links` is the project's `ListingLinks`; an unqualified `label:` is
    looked up in `default_listing`.

    When a listing row carries the label, the translated link also names
    that row's copy (`?move=NAME` or `?unmoved`), so a label in one of
    several overlapping copies links to its own copy.
    """
    def rewrite(match):
        name = match.group("name")
        at = match.group("at") or ""
        flag = match.group("flag") or ""
        key = resolve_listing_key(at[1:], default_listing)
        addr = links.label_addr(key, name) if key is not None else None
        if addr is None:
            src = f" ({source_label})" if source_label else ""
            where = f"@{key}" if key else at
            print(f"  Warning: label:{name}{where} — unknown label{src}")
            return match.group(0)
        flags = parse_link_flags(flag[1:])
        copy_flags = links.label_flags(key, name)
        if copy_flags is not None and not flags.selects_copy:
            flags.move = copy_flags.move
            flags.unmoved = copy_flags.unmoved
            flag = f"?{flags}"
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


def _apply_address_links(md_text, address_links, links=None):
    """Insert Markdown links for address references before HTML conversion.

    Each entry in address_links specifies a pattern to match, which
    occurrence to link, and the target listing (`version`, a listing
    key) and address for the anchor. Replacements are applied
    end-to-start so positions don't shift.

    If `links` (the project's `ListingLinks`) is provided, addresses
    that don't have a direct anchor are resolved to the nearest
    preceding anchor address.
    """
    replacements = []

    for link_spec in address_links:
        pattern = link_spec["pattern"]
        occurrence = link_spec["occurrence"]
        key = link_spec["version"]
        address = int(link_spec["address"], 0)

        resolution = None
        if links is not None and key in links:
            resolution = links.listing_anchors(key).resolve(address)
        fragment = resolution.id if resolution else anchor_id(address)
        url = f"{listing_page_stem(key)}.html#{fragment}"

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


def _render_document_markdown(md_text):
    """Render a standalone Markdown document (a per-version doc or a
    project analysis) to HTML, with GitHub-style heading ids so that
    `#heading` links written for GitHub work on the site too."""
    converter = markdown_lib.Markdown(extensions=["tables", "fenced_code"])
    return add_heading_ids(converter.convert(md_text))


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
                      output_dirpath, links, pages=None,
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
            md_text = _apply_address_links(md_text, address_links, links)

        content_html = _render_document_markdown(md_text)

        doc_filename = _doc_output_filename(version_id, doc["path"])
        src_label = f"{source['slug']}/{doc_filename}"

        # Inline [text](glossary:SLUG) references.
        content_html = apply_glossary_uri_links(
            content_html, glossary_slug_lookup, source_label=src_label)

        # Inline [text](label:NAME[@listing]) references translate to the
        # equivalent address: URI, resolved below. Unqualified label: /
        # address: URIs default to this doc's version's primary listing.
        default_listing = listing_key(version_id)
        content_html = apply_label_uri_links(
            content_html, links, default_listing=default_listing,
            source_label=src_label)

        # Rewrite inline [label](address:HEX[@listing]) URIs.
        content_html = apply_address_uri_links(
            content_html, links, default_listing=default_listing,
            source_label=src_label)

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


def _render_additional_disassemblies(env, source, plan, output_dirpath,
                                     links, glossary_slug_lookup=None,
                                     pages=None, updated_iso=None,
                                     updated_display=None):
    """Render each additional listing declared for a version.

    A version can comprise more than one separately-disassembled body of
    code. JOYSTIK carries two 256-byte driver variants disassembled apart
    from its main image; a disc holds several separate programs. Each is
    declared under `disassemblies` in the version metadata (label, slug,
    json) and gets its own formatted, anchored listing page (and a
    memory-map page when the listing carries one), exactly like the
    primary disassembly.

    Navigation links each listing to the version's primary listing and
    across to the others. A version of peer programs (see
    `_VersionPlan.peers`) names the primary by its own label; otherwise
    it reads as the parent program's disassembly.
    """
    if not plan.extras:
        return

    disassembly_template = env.get_template("_disassembly.html")
    slug = source["slug"]
    name = source["name"]
    description = source["description"]
    source_addr_label = _source_address_label(plan.meta_filepath,
                                              plan.rom_meta)

    for listing in plan.extras:
        data = listing.data
        page_id = listing.page_stem

        # Navigation: to the primary listing, then across to the others,
        # then this listing's own memory map (if any).
        nav = []
        if plan.primary is not None:
            nav.append(_listing_link(
                plan.primary,
                None if plan.peers else f"{plan.title} disassembly"))
        nav.extend(_listing_link(other) for other in plan.extras
                   if other is not listing)
        if listing.has_memory_map:
            nav.append(_memory_map_link(listing))
        link_groups = _group_links(nav)

        anchors = listing.anchors
        sections = process_disassembly(
            data, version_id=page_id, glossary_lookup=glossary_slug_lookup,
            anchors=anchors, links=links, listing_key=listing.key)
        macros = build_macros(data)

        html = disassembly_template.render(
            root="../",
            slug=slug,
            name=name,
            version_id=page_id,
            title=listing.label,
            context=plan.title if plan.peers else None,
            description=listing.entry.get("runtime", description),
            link_groups=link_groups,
            sections=sections,
            macros=macros,
            subroutines=_filter_subroutines(data, anchors),
            updated_iso=updated_iso,
            updated_display=updated_display,
            source_addr_label=source_addr_label,
            files=_file_rows(listing.files),
        )
        (output_dirpath / f"{page_id}.html").write_text(html)
        print(f"  {slug}/{page_id}.html")
        if pages is not None:
            pages.append({
                "url": f"{BASE_URL}{slug}/{page_id}.html",
                "title": (f"{listing.label}, {plan.title}" if plan.peers
                          else listing.label),
                "description": description,
                "is_disassembly": True,
                "updated": updated_iso,
            })

        # A memory-map page too, if this listing enriched any non-ROM
        # labels with memory-map metadata.
        _render_listing_memory_map(env, source, plan, listing, listing.label,
                                   output_dirpath, links, pages,
                                   glossary_slug_lookup)


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
        fragment = match.group(2) or ""
        stem = Path(href).stem.lower()
        if stem in known_stems:
            return f'href="{stem}.html{fragment}"'
        return match.group(0)

    # Match href="..." where the target ends in .md and isn't an
    # absolute URL (no scheme, no leading //). A `#heading` fragment is
    # kept: the rendered page has the same GitHub-style heading ids.
    return re.sub(r'href="((?!https?:|//)[^"#?]+\.md)(#[^"]*)?"', repl, html)


def _render_analysis_pages(env, source, output_dirpath, links,
                           pages=None, glossary_slug_lookup=None,
                           source_page_map=None):
    """Build project-level analysis pages from acornaeology.json.

    Each entry in `source["analyses"]` points to a Markdown file
    inside the source repo and is rendered to a standalone HTML
    page at `<slug>/<stem>.html`. Inline `address:` / `label:` /
    `glossary:` links behave as for per-version docs (analyses are
    project-level, so `address:` / `label:` must name a listing with
    `@version` or `@version/slug`).
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
            md_text = _apply_address_links(md_text, address_links, links)

        content_html = _render_document_markdown(md_text)

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

        # Inline [text](label:NAME@listing) references. Analyses are
        # project-level, so label: (like address:) must name a listing.
        content_html = apply_label_uri_links(
            content_html, links, default_listing=None,
            source_label=src_label)

        # Rewrite inline [label](address:HEX@listing) URIs. Analyses
        # are project-level, so there's no implicit current listing;
        # authors must always name one. apply_address_uri_links warns on
        # unqualified references rather than guessing.
        content_html = apply_address_uri_links(
            content_html, links, default_listing=None,
            source_label=src_label)

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


def _render_memory_map_page(env, source, key, version_title,
                            memory_map, output_dirpath, links,
                            pages=None, group_titles=None,
                            rom_load_addr=None, rom_end_addr=None,
                            regions=None, glossary_slug_lookup=None):
    """Render the memory-map page of listing `key`
    (`{page stem}-memory-map.html`).

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
    the listing's `{page stem}.html#addr-XXXX` (code) or `#mm-NAME` (other
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
            hex_str, at, flag_str, label = match.groups()
            # Only intercept unqualified URIs (no @listing). URIs naming
            # a listing still go to the listing resolver, which also
            # handles multi-version anchor disambiguation.
            if at:
                return match.group(0)
            addr = int(hex_str, 16)
            name = mm_addr_to_name.get(addr)
            if name is None:
                return match.group(0)
            flags = parse_link_flags(flag_str)
            # A link naming a copy of moved code (or with an unusable
            # flag) is left to the listing resolver.
            if flags.error or flags.move or flags.stored:
                return match.group(0)
            url = f"#mm-{name}"
            # `mm-link` is the cyan "memory-location reference" class;
            # used here for same-page jumps between memory-map entries
            # and on the listing page for cross-window navigation. The
            # shared class gives the two views a consistent palette:
            # memory locations read cyan regardless of which page the
            # reader is on.
            if not flags.hex:
                return f'<a class="mm-link" href="{url}">{label}</a>'
            hex_display = f'<code>&amp;{hex_str.upper()}</code>'
            return (f'<a class="mm-link" href="{url}">{label}</a> '
                    f'(<a class="mm-link" href="{url}">{hex_display}</a>)')
        return _ADDRESS_URI_RE.sub(repl, html)

    page_stem = listing_page_stem(key)
    output_filename = f"{page_stem}-memory-map.html"

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
            html, links, default_listing=key,
            source_label=f"{source['slug']}/{output_filename}")
        html = rewrite_mm_refs(html)
        html = apply_address_uri_links(
            html, links, default_listing=key,
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
        version_id=page_stem,
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


def _filter_subroutines(data, anchors):
    """Return ROM-range subroutine and banner entries, in address order.

    The version-page sidebar TOC lists everything that has its own
    ``<div class="sub-header">`` block in the listing — so both real
    subroutines (``data["subroutines"]``) and standalone banners
    (``data["banners"]`` — dasmos's analogue of py8dis's
    ``data_banner``). Both share the same shape (``addr``, ``name``,
    ``title``, ``description``) and the same TOC styling, so they
    merge cleanly here.

    Each entry is returned as a copy with an `anchor` key, the id of its
    row in the listing (from `anchors`, the listing's `AnchorIndex`), for
    the sidebar link.
    """
    meta = data.get("meta", {})
    load_addr = meta.get("load_addr", 0)
    end_addr = meta.get("end_addr", 0xFFFF)
    combined = list(data.get("subroutines", [])) + list(data.get("banners", []))
    in_range = [s for s in combined if load_addr <= s["addr"] < end_addr]
    in_range.sort(key=lambda s: s["addr"])
    return [
        {**s, "anchor": anchors.entry_link_id(s)}
        for s in in_range
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


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sources", dest="sources_filepath", type=Path,
        default=DEFAULT_SOURCES_FILEPATH,
        help="JSON list of disassembly sources to build (default: "
             "data/sources.json). Relative local paths in it resolve "
             "against its own directory, so a private sources file can "
             "add unpublished checkouts without editing the committed one.")
    return parser.parse_args(argv)


def main(argv=None):
    args = _parse_args(argv)
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
    sources = load_sources(args.sources_filepath.resolve())

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
