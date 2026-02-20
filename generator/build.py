#!/usr/bin/env python3
"""Static site builder for acornaeology.uk"""

import json
import shutil
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from .disassembly import process_disassembly


REPO_ROOT = Path(__file__).resolve().parent.parent
SITE_DIRPATH = REPO_ROOT / "site"
TEMPLATES_DIRPATH = REPO_ROOT / "templates"
DATA_DIRPATH = REPO_ROOT / "data"
OUTPUT_DIRPATH = REPO_ROOT / "output"


def is_page_template(filepath):
    """A page template extends a base — non-page templates are skipped.

    Templates prefixed with _ are data-driven and rendered separately."""
    if filepath.name.startswith("_"):
        return False
    content = filepath.read_text()
    return "{% extends" in content


def build_templates(env):
    """Render all page templates to the output directory."""
    for template_filepath in TEMPLATES_DIRPATH.glob("*.html"):
        if not is_page_template(template_filepath):
            continue
        template = env.get_template(template_filepath.name)
        # Calculate root path (relative URL prefix to site root)
        output_filepath = OUTPUT_DIRPATH / template_filepath.name
        html = template.render(root="./")
        output_filepath.write_text(html)
        print(f"  {template_filepath.name} -> {output_filepath.relative_to(REPO_ROOT)}")


def build_disassemblies(env):
    """Build disassembly pages from the data/ directory."""
    if not DATA_DIRPATH.is_dir():
        return

    rom_index_template = env.get_template("_rom_index.html")
    disassembly_template = env.get_template("_disassembly.html")

    for project_dirpath in sorted(DATA_DIRPATH.iterdir()):
        if not project_dirpath.is_dir():
            continue
        meta_filepath = project_dirpath / "meta.json"
        if not meta_filepath.exists():
            continue

        meta = json.loads(meta_filepath.read_text())
        slug = meta["slug"]

        # Create output directory
        output_dirpath = OUTPUT_DIRPATH / slug
        output_dirpath.mkdir(parents=True, exist_ok=True)

        # Build per-ROM index page
        html = rom_index_template.render(
            root="../",
            name=meta["name"],
            description=meta.get("description", ""),
            versions=meta["versions"],
        )
        index_filepath = output_dirpath / "index.html"
        index_filepath.write_text(html)
        print(f"  {slug}/index.html")

        # Build per-version disassembly pages
        for version in meta["versions"]:
            data_filepath = project_dirpath / version["filename"]
            data = json.loads(data_filepath.read_text())

            lines = process_disassembly(data)

            html = disassembly_template.render(
                root="../",
                title=f"{meta['name']} {version['id']}",
                description=meta.get("description", ""),
                lines=lines,
                subroutines=data.get("subroutines", []),
            )

            version_filepath = output_dirpath / f"{version['id']}.html"
            version_filepath.write_text(html)
            print(f"  {slug}/{version['id']}.html")


def copy_static():
    """Copy static assets (CSS, fonts, images) to the output directory."""
    for subdir in ("css", "fonts"):
        src = SITE_DIRPATH / subdir
        dst = OUTPUT_DIRPATH / subdir
        if src.is_dir():
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
            print(f"  {subdir}/")


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

    # Render templates
    print("Pages:")
    build_templates(env)

    # Build disassembly pages
    print("Disassemblies:")
    build_disassemblies(env)

    print("Done.")


if __name__ == "__main__":
    main()
