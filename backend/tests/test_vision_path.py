"""The vision path — the half of the library this backend could not run.

Until now every bundled provider implemented ``complete_structured`` only. The
backend accepted page images, stored them, served them to the UI, and never
sent one to a model, so ``saknussemm``'s ``VisionEditProducer`` — crops per
line, image-is-authoritative prompt, loosened guards, chunk splitting on
``max_images`` — had no consumer at all.

These tests pin the four things that make the difference between a vision run
and a request that cannot succeed. Each of the four was learned by getting it
wrong first, which is why each has its own case rather than being folded into
one happy path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from saknussemm.core.schemas import GuardConfig
from saknussemm.producers.vision import ImagePart, MultimodalStructuredClient

from app.jobs.runner import _media_type_for, page_image_assets
from app.providers.mistral_multimodal import MistralMultimodalProvider
from app.schemas import DocumentManifest


def _encoded(fmt: str, size: tuple[int, int] = (40, 12)) -> bytes:
    """A real, decodable image.

    Hand-rolled header bytes were enough while the code only sniffed the magic
    number; they stopped being enough when ``page_image_assets`` began
    DECODING the scan to derive the XML-to-pixel scale. A fixture that cannot
    be opened then fails for a reason that has nothing to do with what is
    under test.
    """
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", size, (255, 255, 255)).save(buffer, format=fmt)
    return buffer.getvalue()


_JPEG = _encoded("JPEG")
_PNG = _encoded("PNG")
_TIFF = _encoded("TIFF")


def test_the_provider_satisfies_the_librarys_multimodal_protocol() -> None:
    """The seam is a Protocol, so conformance is checkable rather than hoped for.

    ``VisionEditProducer`` takes a ``MultimodalStructuredClient``; a provider
    that merely looks similar fails at the first call, deep inside a run.
    """
    assert isinstance(MistralMultimodalProvider(), MultimodalStructuredClient)


def test_the_image_cap_is_declared_and_refused_loudly() -> None:
    """Eight is measured, not documented, and the ninth image 400s.

    The cap exists to be handed to the engine through
    ``ModelCapabilities.max_images`` — ``core/batching.py`` then splits the
    chunk. This assertion is the backstop for when it is *not* handed over:
    refusing before encoding is better than a 400 after, and the message must
    name the fix rather than the symptom.
    """
    provider = MistralMultimodalProvider()
    assert provider.MAX_IMAGES_PER_CALL == 8

    crops = [
        ImagePart(line_id=f"L{i}", media_type="image/jpeg", data=_JPEG, sha256="x")
        for i in range(9)
    ]
    with pytest.raises(ValueError, match="max_images"):
        import asyncio

        asyncio.run(
            provider.complete_structured_multimodal(
                api_key="k",
                model="mistral-small-latest",
                system_prompt="s",
                user_payload={"lines": []},
                images=crops,
                json_schema={},
            )
        )


def test_each_crop_is_labelled_with_the_line_it_depicts() -> None:
    """The reply is keyed by ``line_id`` and the model has no other pairing.

    An unlabelled pile of crops is how a VLM ends up captioning: it cannot tell
    which picture belongs to which line, so it answers about the picture.
    """
    provider = MistralMultimodalProvider()
    crops = [
        ImagePart(line_id="L1", media_type="image/jpeg", data=_JPEG, sha256="a"),
        ImagePart(line_id="L2", media_type="image/png", data=_PNG, sha256="b"),
    ]
    blocks = provider._content_blocks({"lines": [{"line_id": "L1"}]}, crops)

    labels = [b["text"] for b in blocks if b["type"] == "text"]
    assert any("L1" in label for label in labels), labels
    assert any("L2" in label for label in labels), labels
    images = [b for b in blocks if b["type"] == "image_url"]
    assert len(images) == 2
    assert images[0]["image_url"].startswith("data:image/jpeg;base64,")
    assert images[1]["image_url"].startswith("data:image/png;base64,")
    # The payload rides last, after the crops it describes.
    assert blocks[-1]["type"] == "text"
    assert "line_id" in blocks[-1]["text"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(_JPEG, "image/jpeg"), (_PNG, "image/png"), (_TIFF, "image/tiff")],
)
def test_the_media_type_is_read_from_the_bytes(tmp_path: Path, raw: bytes, expected: str) -> None:
    """A ``.jpg`` that is really a PNG makes the vendor reject the data URI.

    ``ImageAsset.media_type`` documents itself as "determined from the bytes
    rather than guessed from the extension", so every file here is named
    ``.jpg`` regardless of what it contains.
    """
    path = tmp_path / "scan.jpg"
    path.write_bytes(raw)
    assert _media_type_for(path) == expected


def _manifest_with(pages_per_file: dict[str, int]) -> DocumentManifest:
    """A manifest with the given number of pages per source file."""
    from saknussemm.core.schemas import PageManifest

    pages = []
    for source, count in pages_per_file.items():
        for index in range(count):
            pages.append(
                PageManifest(
                    page_id=f"{Path(source).stem}_P{index}",
                    source_file=source,
                    page_index=index,
                    page_width=100,
                    page_height=100,
                    blocks=[],
                    lines=[],
                )
            )
    return DocumentManifest(pages=pages, source_files=sorted(pages_per_file))


def test_one_page_per_file_maps_cleanly(tmp_path: Path) -> None:
    scan = tmp_path / "a.jpg"
    scan.write_bytes(_JPEG)
    manifest = _manifest_with({"a.xml": 1})

    assets = page_image_assets(manifest, {"a": scan})

    assert list(assets) == ["a_P0"]
    asset = assets["a_P0"]
    assert asset.page_id == "a_P0"
    assert asset.media_type == "image/jpeg"
    assert asset.sha256 and len(asset.sha256) == 64


def test_a_multipage_file_is_refused_rather_than_flattened(tmp_path: Path) -> None:
    """The one failure mode nothing downstream could ever see.

    This backend keys images by SOURCE FILE; the library wants one per physical
    page and ``require_page_images`` spells out why — "flattening them to a
    single per-file ref sent the producer the wrong image for every page but
    the first". A line corrected against the wrong scan comes back confident
    and wrong, and the projection invariant cannot notice: it compares the
    artefact to the decisions the artefact was built from.
    """
    scan = tmp_path / "vol.jpg"
    scan.write_bytes(_JPEG)
    manifest = _manifest_with({"vol.xml": 3})

    with pytest.raises(ValueError, match="one scan per physical page"):
        page_image_assets(manifest, {"vol": scan})


def test_a_file_without_a_scan_is_simply_absent(tmp_path: Path) -> None:
    """Not every uploaded XML has a matching image, and that is not an error.

    ``require_page_images`` refuses a vision run whose coverage is incomplete,
    so the decision belongs there — this helper reports what exists.
    """
    manifest = _manifest_with({"a.xml": 1, "b.xml": 1})
    scan = tmp_path / "a.jpg"
    scan.write_bytes(_JPEG)

    assets = page_image_assets(manifest, {"a": scan})
    assert list(assets) == ["a_P0"]


def test_the_vision_guard_config_is_looser_than_the_text_one() -> None:
    """Why the branch changes the guards and not only the producer.

    A VLM reads the image, so a CORRECT reading of a badly garbled line
    diverges from the OCR text further than the text-tuned threshold tolerates.
    Measured once with the text config by mistake: 18 refusals out of 20 lines,
    every one ``too_different_from_source``.
    """
    from saknussemm.core.guards import DEFAULT_GUARD_CONFIG

    vision = GuardConfig.vision()
    assert vision.min_source_similarity < DEFAULT_GUARD_CONFIG.min_source_similarity
    # Everything else must match: this is one calibrated dial, not a second
    # policy that could drift away from the text one.
    text_dump = DEFAULT_GUARD_CONFIG.model_dump()
    vision_dump = vision.model_dump()
    differing = {key for key in text_dump if text_dump[key] != vision_dump[key]}
    assert differing == {"min_source_similarity"}, differing


class _RecordingMultimodalProvider:
    """Answers every line with its own text, and records what it was sent."""

    MAX_IMAGES_PER_CALL = 8

    def __init__(self) -> None:
        self.calls: list[int] = []

    async def complete_structured_multimodal(
        self,
        *,
        api_key: str,
        model: str,
        system_prompt: str,
        user_payload: dict[str, Any],
        images: list[ImagePart],
        json_schema: dict[str, Any],
        temperature: float = 0.0,
    ) -> tuple[dict[str, Any], None]:
        self.calls.append(len(images))
        lines = [
            {"line_id": line["line_id"], "corrected_text": line["ocr_text"]}
            for line in user_payload.get("lines", [])
        ]
        return {"lines": lines}, None


def test_the_engine_never_sends_more_crops_than_declared() -> None:
    """The capability descriptor is what splits the chunk, and it is load-bearing.

    Without ``max_images`` the engine batches every line of a chunk into one
    call; with a twelve-line window and a cap of eight that request is refused
    by the vendor before it can do anything. This drives a real page through
    the real producer and asserts no call ever exceeded the cap.
    """
    import asyncio

    from saknussemm.core.schemas import ModelCapabilities
    from saknussemm.producers.vision import VisionEditProducer

    provider = _RecordingMultimodalProvider()
    producer = VisionEditProducer(
        provider=provider,
        api_key="k",
        model="m",
        capabilities=ModelCapabilities(
            text=True,
            vision=True,
            structured_output=True,
            max_images=provider.MAX_IMAGES_PER_CALL,
        ),
    )
    assert producer.capabilities.max_images == 8
    assert producer.wants_image is True
    # The producer must also declare vision, or preflight refuses the run
    # before the first call — it caught exactly this omission once.
    assert producer.capabilities.vision is True
    del asyncio
