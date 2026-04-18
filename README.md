# acornaeology.uk

[![Deploy to GitHub Pages](https://github.com/acornaeology/acornaeology.github.io/actions/workflows/deploy.yml/badge.svg)](https://github.com/acornaeology/acornaeology.github.io/actions/workflows/deploy.yml)

Static site generator for [acornaeology.uk](https://acornaeology.uk), a collection of resources for exploring the software and hardware of Acorn Computers.

## How it works

The site is built from two kinds of content:

- **Page templates** in `templates/` — static pages like the home page and about page, rendered with [Jinja2](https://jinja.palletsprojects.com/).
- **Disassembly data** from external repositories — each disassembly project lives in its own repo (e.g. [acorn-nfs](https://github.com/acornaeology/acorn-nfs)) and is pulled in at build time.

### Disassembly sources

External disassembly repos are listed in `data/sources.json`:

```json
[
    {
        "repo": "https://github.com/acornaeology/acorn-nfs",
        "path": "../../acorn-nfs"
    }
]
```

Each entry has a `repo` URL and an optional local `path` for development. During the build, the generator looks for a local checkout first. If none is found, it shallow-clones the repo into `.cache/`.

Each disassembly repo provides an `acornaeology.json` manifest at its root, describing the project name, slug, and available versions. Per-version metadata (title, links) comes from `rom.json` within each version directory. The generator reads all of this to produce the formatted disassembly pages.

## Project structure

```
templates/          Jinja2 page templates
generator/          Python site generator
  build.py          Main build script
  disassembly.py    Transforms disassembly JSON into HTML
site/               Static assets (CSS, fonts, images)
data/               Build configuration (sources.json)
output/             Generated site (gitignored)
```

## Building locally

Requires [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run python -m generator.build
```

The generated site is written to `output/`.

For local development, check out the disassembly repos as siblings of this repo so the `path` entries in `sources.json` resolve correctly.

## Deployment

A GitHub Actions workflow builds and deploys the site to GitHub Pages on every push to `master`. It can also be triggered manually via `workflow_dispatch`. The site is served from the custom domain [acornaeology.uk](https://acornaeology.uk).

## Adding a new disassembly

1. Create a new disassembly repo with an `acornaeology.json` manifest and per-version `rom.json` metadata.
2. Add an entry to `data/sources.json` with the repo URL and optional local path.
3. Rebuild the site.

## Linkable addresses in Markdown writeups

Per-version docs (`rom.json.docs`) and project-level analyses (`acornaeology.json.analyses`) can use an `address:` URI scheme inside regular Markdown link syntax to turn a label into a clickable disassembly anchor:

```markdown
[rx_frame_b](address:E263)             ← defaults to the current doc's version
[rx_frame_b (&E263)](address:E263)     ← label with hex in it, same target
[legacy init](address:80F3@3.60)       ← explicit version (required in analyses)
```

The `address:` scheme is resolved at render time:

- Inside a **per-version doc**, an unqualified URI (`address:E263`) links to that doc's own version. Use `@version` to link to a different version.
- Inside a **project-level analysis**, authors must always specify `@version` — analyses aren't tied to a single version, so there's no implicit default.
- Hex is 4+ digits, case-insensitive. If the exact address isn't an anchor in the target version, the link falls back to the nearest preceding anchor.

Unresolvable URIs (unknown version, missing `@version` in an analysis, address before the first anchor) print a build warning and leave the link unchanged rather than failing the build.

## Author

[Robert Smallshire](https://github.com/rob-smallshire)

## License

MIT
