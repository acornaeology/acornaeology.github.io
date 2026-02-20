#!/usr/bin/env python3
"""Static site builder for acornaeology.uk"""

import shutil
from pathlib import Path

from jinja2 import Environment, FileSystemLoader


REPO_ROOT = Path(__file__).resolve().parent.parent
SITE_DIRPATH = REPO_ROOT / "site"
TEMPLATES_DIRPATH = REPO_ROOT / "templates"
OUTPUT_DIRPATH = REPO_ROOT / "output"


def is_page_template(filepath):
    """A page template extends a base — non-page templates are skipped."""
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

    print("Done.")


if __name__ == "__main__":
    main()
