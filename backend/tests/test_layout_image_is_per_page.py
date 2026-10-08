"""One uploaded scan is one physical page, never a whole multipage XML.

The review queue crops the page image at the line's coordinates. With the
image attached per source file, every page of a two-page ALTO showed the
same scan, and a reviewer could accept the text of page 2 while looking at
page 1 (reproduced over HTTP in the 2026-10-07 review). The layout now
attaches an image only to a page that is alone in its source file.
"""

from __future__ import annotations

from pathlib import Path

from saknussemm.formats.alto.parser import build_document_manifest

from app.api.read_models import build_layout

_NS = "http://www.loc.gov/standards/alto/ns-v3#"


def _page(n: int, text: str) -> str:
    return (
        f'<Page ID="P{n}" PHYSICAL_IMG_NR="{n}" WIDTH="100" HEIGHT="100">'
        '<PrintSpace HPOS="0" VPOS="0" WIDTH="100" HEIGHT="100">'
        f'<TextBlock ID="B{n}" HPOS="5" VPOS="5" WIDTH="90" HEIGHT="15">'
        f'<TextLine ID="L{n}" HPOS="5" VPOS="5" WIDTH="90" HEIGHT="15">'
        f'<String ID="W{n}" CONTENT="{text}" HPOS="5" VPOS="5" WIDTH="90" HEIGHT="15"/>'
        "</TextLine></TextBlock></PrintSpace></Page>"
    )


def _alto(*pages: str) -> bytes:
    return (
        f'<alto xmlns="{_NS}"><Description><MeasurementUnit>pixel</MeasurementUnit>'
        "<sourceImageInformation><fileName>vol.png</fileName></sourceImageInformation>"
        f"</Description><Layout>{''.join(pages)}</Layout></alto>"
    ).encode()


def test_a_multipage_file_with_one_scan_shows_no_image_on_any_page(tmp_path: Path):
    path = tmp_path / "vol.xml"
    path.write_bytes(_alto(_page(1, "PREMIERE"), _page(2, "DEUXIEME")))
    doc = build_document_manifest([(path, "vol.xml")])
    layout = build_layout("job", doc, {"vol.xml": "vol.png"})
    assert [p["page_id"] for p in layout["pages"]] == ["P1", "P2"]
    assert [p["image_url"] for p in layout["pages"]] == [None, None]


def test_a_single_page_file_keeps_its_scan(tmp_path: Path):
    path = tmp_path / "vol.xml"
    path.write_bytes(_alto(_page(1, "PREMIERE")))
    doc = build_document_manifest([(path, "vol.xml")])
    layout = build_layout("job", doc, {"vol.xml": "vol.png"})
    assert layout["pages"][0]["image_url"] == "/api/jobs/job/images/vol.png"


def test_one_file_per_page_keeps_every_scan(tmp_path: Path):
    first = tmp_path / "p1.xml"
    second = tmp_path / "p2.xml"
    first.write_bytes(_alto(_page(1, "PREMIERE")))
    second.write_bytes(_alto(_page(1, "DEUXIEME")))
    doc = build_document_manifest([(first, "p1.xml"), (second, "p2.xml")])
    layout = build_layout("job", doc, {"p1.xml": "p1.png", "p2.xml": "p2.png"})
    assert [p["image_url"] for p in layout["pages"]] == [
        "/api/jobs/job/images/p1.png",
        "/api/jobs/job/images/p2.png",
    ]
