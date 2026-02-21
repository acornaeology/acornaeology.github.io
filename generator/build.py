#!/usr/bin/env python3
"""Static site builder for acornaeology.uk"""

import json
import shutil
import subprocess
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from .disassembly import process_disassembly


REPO_ROOT = Path(__file__).resolve().parent.parent
SITE_DIRPATH = REPO_ROOT / "site"
TEMPLATES_DIRPATH = REPO_ROOT / "templates"
DATA_DIRPATH = REPO_ROOT / "data"
OUTPUT_DIRPATH = REPO_ROOT / "output"
CACHE_DIRPATH = REPO_ROOT / ".cache"


def is_page_template(filepath):
    """A page template extends a base — non-page templates are skipped.

    Templates prefixed with _ are data-driven and rendered separately."""
    if filepath.name.startswith("_"):
        return False
    content = filepath.read_text()
    return "{% extends" in content


def build_templates(env, roms):
    """Render all page templates to the output directory."""
    for template_filepath in TEMPLATES_DIRPATH.glob("*.html"):
        if not is_page_template(template_filepath):
            continue
        template = env.get_template(template_filepath.name)
        output_filepath = OUTPUT_DIRPATH / template_filepath.name
        html = template.render(root="./", roms=roms)
        output_filepath.write_text(html)
        print(f"  {template_filepath.name} -> {output_filepath.relative_to(REPO_ROOT)}")


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
        ["git", "clone", "--depth", "1", repo_url, str(clone_dirpath)],
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

        result.append({
            "repo_dirpath": repo_dirpath,
            "repo_url": repo_url,
            "slug": manifest["slug"],
            "name": manifest["name"],
            "description": manifest.get("description", ""),
            "versions": manifest["versions"],
        })

    return result


def build_disassemblies(env, sources):
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

        # Build version metadata for the index page
        versions = []
        for version_id in source["versions"]:
            rom_json_filepath = repo_dirpath / "versions" / version_id / "rom" / "rom.json"
            if rom_json_filepath.exists():
                rom_meta = json.loads(rom_json_filepath.read_text())
                title = rom_meta.get("title", f"{name} {version_id}")
            else:
                title = f"{name} {version_id}"
            versions.append({"id": version_id, "title": title})

        # Build per-ROM index page
        html = rom_index_template.render(
            root="../",
            slug=slug,
            name=name,
            description=description,
            versions=versions,
        )
        index_filepath = output_dirpath / "index.html"
        index_filepath.write_text(html)
        print(f"  {slug}/index.html")

        # Build per-version disassembly pages
        for version_id in source["versions"]:
            version_dirpath = repo_dirpath / "versions" / version_id

            # Find the disassembly JSON
            output_json_dirpath = version_dirpath / "output"
            json_files = list(output_json_dirpath.glob("*.json"))
            if not json_files:
                print(f"  Warning: no JSON found in {output_json_dirpath}, skipping")
                continue
            data_filepath = json_files[0]
            data = json.loads(data_filepath.read_text())

            # Read version metadata
            rom_json_filepath = version_dirpath / "rom" / "rom.json"
            if rom_json_filepath.exists():
                rom_meta = json.loads(rom_json_filepath.read_text())
                title = rom_meta.get("title", f"{name} {version_id}")
                links = list(rom_meta.get("links", []))
            else:
                title = f"{name} {version_id}"
                links = []

            # Prepend the GitHub link
            github_link = {
                "label": "Disassembly source on GitHub",
                "url": repo_url,
                "icon": "github",
            }
            links.insert(0, github_link)

            lines = process_disassembly(data)

            html = disassembly_template.render(
                root="../",
                slug=slug,
                version_id=version_id,
                title=title,
                description=description,
                links=links,
                lines=lines,
                subroutines=_filter_subroutines(data),
            )

            version_filepath = output_dirpath / f"{version_id}.html"
            version_filepath.write_text(html)
            print(f"  {slug}/{version_id}.html")


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

    # Render templates
    print("Pages:")
    build_templates(env, sources)

    # Build disassembly pages
    print("Disassemblies:")
    build_disassemblies(env, sources)

    print("Done.")


if __name__ == "__main__":
    main()
