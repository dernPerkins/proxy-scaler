"""CLI for proxy-scaler: decklist → Scryfall → upscale → PNG output."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .decklist import parse_decklist
from .dpi import DEFAULT_DPI, DPI_OPTIONS
from .pipeline import process_entries
from .upscale import UpscaleModel


def build_parser() -> argparse.ArgumentParser:
    model_choices = [m.value for m in UpscaleModel]
    p = argparse.ArgumentParser(
        prog="proxy-scaler",
        description=(
            "Fetch MTG card images from Scryfall and upscale them locally "
            "for home proxy printing (e.g. upload to proxxied)."
        ),
    )
    p.add_argument(
        "decklist",
        type=Path,
        help="Path to decklist file ({qty} {name} or {qty} {name} ({set}) {collector})",
    )
    p.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("output"),
        help="Directory for upscaled PNGs (default: output/)",
    )
    p.add_argument(
        "--model",
        choices=model_choices,
        default=UpscaleModel.SWINIR.value,
        help=(
            "Upscale model: swinir (default, fidelity-first), realesrnet, "
            "or realesrgan. RealESRNet is x4-native only."
        ),
    )
    p.add_argument(
        "--dpi",
        type=int,
        choices=DPI_OPTIONS,
        default=DEFAULT_DPI,
        help="Target print DPI: 600, 800, or 1200 (default)",
    )
    p.add_argument(
        "--all-dpis",
        action="store_true",
        help="Generate 600, 800, and 1200 DPI variants for each face",
    )
    p.add_argument(
        "--cache-dir",
        type=Path,
        default=Path("imgcache"),
        help="Cache directory for upscaled images (default: imgcache/)",
    )
    p.add_argument(
        "--weights-dir",
        type=Path,
        default=Path("weights"),
        help="Directory for model weights (default: weights/)",
    )
    p.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip writing output files that already exist",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not args.decklist.is_file():
        print(f"error: decklist not found: {args.decklist}", file=sys.stderr)
        return 1

    entries = parse_decklist(args.decklist)
    if not entries:
        print("error: no card entries found in decklist", file=sys.stderr)
        return 1

    try:
        result = process_entries(
            entries,
            output_dir=args.output,
            dpi=args.dpi,
            all_dpis=args.all_dpis,
            model=args.model,
            cache_dir=args.cache_dir,
            weights_dir=args.weights_dir,
            skip_existing=args.skip_existing,
            on_progress=print,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    return 1 if result.failed and not result.wrote else 0


if __name__ == "__main__":
    raise SystemExit(main())
