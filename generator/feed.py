"""Sitemap and Atom feed generation."""

import xml.etree.ElementTree as ET
from datetime import datetime, timezone


SITEMAP_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"
ATOM_NS = "http://www.w3.org/2005/Atom"

ET.register_namespace("", SITEMAP_NS)
ET.register_namespace("", ATOM_NS)


def generate_sitemap(pages, output_filepath):
    """Write a sitemap.xml listing all site pages."""
    urlset = ET.Element("urlset", xmlns=SITEMAP_NS)
    for page in pages:
        url_el = ET.SubElement(urlset, "url")
        ET.SubElement(url_el, "loc").text = page["url"]

    tree = ET.ElementTree(urlset)
    ET.indent(tree)
    tree.write(output_filepath, xml_declaration=True, encoding="UTF-8")


def generate_atom_feed(pages, output_filepath, base_url):
    """Write an Atom feed of disassembly pages."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    feed = ET.Element("feed", xmlns=ATOM_NS)
    ET.SubElement(feed, "title").text = "acornaeology"
    ET.SubElement(feed, "subtitle").text = (
        "Software archaeology for Acorn Computer artefacts."
    )
    ET.SubElement(feed, "link", href=base_url)
    ET.SubElement(feed, "link", rel="self", href=base_url + "atom.xml")
    ET.SubElement(feed, "id").text = base_url
    ET.SubElement(feed, "updated").text = now

    for page in pages:
        if not page.get("is_disassembly"):
            continue
        entry = ET.SubElement(feed, "entry")
        ET.SubElement(entry, "title").text = page["title"]
        ET.SubElement(entry, "link", href=page["url"])
        ET.SubElement(entry, "id").text = page["url"]
        ET.SubElement(entry, "updated").text = now
        if page.get("description"):
            ET.SubElement(entry, "summary").text = page["description"]

    tree = ET.ElementTree(feed)
    ET.indent(tree)
    tree.write(output_filepath, xml_declaration=True, encoding="UTF-8")
