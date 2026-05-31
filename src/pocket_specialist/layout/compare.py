"""Side-by-side layout comparison helpers for provider debugging."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw

from pocket_specialist.core.config import dpi_to_zoom
from pocket_specialist.handlers.intake import render_pdf_page_to_bytes
from pocket_specialist.layout.providers import LayoutProvider, LayoutResult, build_layout_provider, crop_region_image

_PROVIDER_SLUGS = {
    "surya-layout-service": "surya-layout-service",
    "pp-doclayout-v3": "pp-doclayout-v3",
    "paddleocr-layout-service": "paddleocr-layout-service",
}

_REGION_COLORS = {
    "heading": "#e63946",
    "text": "#457b9d",
    "formula": "#2a9d8f",
    "table": "#f4a261",
    "figure": "#6a4c93",
    "code": "#264653",
    "header": "#8d99ae",
    "footer": "#8d99ae",
    "key_value": "#ff006e",
    "list": "#118ab2",
}


def _provider_slug(provider_name: str) -> str:
    normalized = provider_name.strip().lower()
    return _PROVIDER_SLUGS.get(normalized, normalized.replace(" ", "-").replace("_", "-"))


def _write_overlay(image_bytes: bytes, layout_result: LayoutResult, *, formula_only: bool = False) -> bytes:
    image = Image.open(BytesIO(image_bytes)).convert("RGB")
    draw = ImageDraw.Draw(image)
    regions = layout_result.regions
    if formula_only:
        regions = [region for region in regions if region.region_type == "formula"]
    for region in regions:
        color = _REGION_COLORS.get(region.region_type, "#222222")
        x0, y0, x1, y1 = region.bbox
        draw.rectangle((x0, y0, x1, y1), outline=color, width=4 if formula_only else 3)
        label = f"{region.region_id}:{region.region_type}:{region.confidence:.2f}" if not formula_only else f"{region.region_id}:{region.confidence:.2f}"
        draw.text((x0 + 2, max(0, y0 - 14)), label, fill=color)
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _report_payload(pdf_path: Path, page_num: int, dpi: int, provider_name: str, layout_result: LayoutResult, provider_status: str) -> dict[str, object]:
    return {
        "pdf_path": str(pdf_path),
        "page_num": page_num,
        "dpi": dpi,
        "provider": provider_name,
        "provider_status": provider_status,
        "layout_confidence": layout_result.layout_confidence,
        "region_count": len(layout_result.regions),
        "region_types": dict(Counter(region.region_type for region in layout_result.regions)),
        "regions": [asdict(region) for region in layout_result.regions],
    }


def compare_layout_page(
    pdf_path: Path,
    *,
    page_num: int,
    dpi: int,
    output_dir: Path,
    providers: list[str] | None = None,
) -> dict[str, object]:
    provider_names = providers or ["pp-doclayout-v3", "paddleocr-layout-service"]
    pdf_path = pdf_path.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    zoom = dpi_to_zoom(dpi)
    image_bytes = render_pdf_page_to_bytes(pdf_path, page_num, zoom=zoom)
    base_image_path = output_dir / f"page_{page_num:04d}.png"
    base_image_path.write_bytes(image_bytes)

    summary: dict[str, object] = {
        "pdf_path": str(pdf_path),
        "page_num": page_num,
        "dpi": dpi,
        "base_image": str(base_image_path),
        "providers": {},
    }

    for provider_name in provider_names:
        provider_slug = _provider_slug(provider_name)
        provider_dir = output_dir / provider_slug
        crops_dir = provider_dir / "crops"
        provider_dir.mkdir(parents=True, exist_ok=True)
        crops_dir.mkdir(parents=True, exist_ok=True)
        provider_summary: dict[str, object] = {"status": "pending"}
        provider: LayoutProvider | None = None
        try:
            provider = build_layout_provider(provider_name)
            if hasattr(provider, "health_check") and not provider.health_check():
                provider_summary["status"] = "unavailable"
                provider_summary["reason"] = "health_check returned false"
                summary["providers"][provider_slug] = provider_summary
                continue
            provider.load()
            layout_result = provider.detect(image_bytes)
            layout_result.page_id = f"page-{page_num:04d}"
            (provider_dir / f"page_{page_num:04d}.json").write_text(
                json.dumps(_report_payload(pdf_path, page_num, dpi, getattr(provider, "name", provider_name), layout_result, "ok"), indent=2),
                encoding="utf-8",
            )
            (provider_dir / f"page_{page_num:04d}_overlay.png").write_bytes(_write_overlay(image_bytes, layout_result))
            (provider_dir / f"page_{page_num:04d}_formula_overlay.png").write_bytes(_write_overlay(image_bytes, layout_result, formula_only=True))
            for region in layout_result.regions:
                crop_path = crops_dir / f"{region.region_id}_{region.region_type}.png"
                crop_path.write_bytes(crop_region_image(image_bytes, region.bbox))
            provider_summary.update(
                {
                    "status": "ok",
                    "provider": getattr(provider, "name", provider_name),
                    "layout_confidence": layout_result.layout_confidence,
                    "region_count": len(layout_result.regions),
                    "region_types": dict(Counter(region.region_type for region in layout_result.regions)),
                    "output_dir": str(provider_dir),
                }
            )
        except Exception as exc:
            provider_summary["status"] = "error"
            provider_summary["reason"] = str(exc)
        finally:
            if provider is not None:
                try:
                    provider.offload()
                except Exception:
                    pass
        summary["providers"][provider_slug] = provider_summary

    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary
