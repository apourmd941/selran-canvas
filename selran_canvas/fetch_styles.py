"""Admin script: pre-fetch all 100 manifest CSL styles from the Zotero repo.

Usage:
    python -m selran_canvas.fetch_styles            # fetch missing only
    python -m selran_canvas.fetch_styles --force    # re-fetch everything
    python -m selran_canvas.fetch_styles --locale en-US  # also fetch a locale

Without this, styles are lazy-fetched on first selection (slower first click but
no upfront network requirement).
"""
from __future__ import annotations

import argparse
import sys

from .csl_index import (
    CSL_STYLES_DIR,
    fetch_style,
    get_locale,
    is_style_local,
    list_styles,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pre-fetch CSL styles from the Zotero repo.")
    parser.add_argument("--force", action="store_true", help="Re-fetch even if file exists.")
    parser.add_argument("--locale", default="en-US", help="Also fetch this locale.")
    parser.add_argument("--category", help="Only fetch styles in this category.")
    args = parser.parse_args(argv)

    CSL_STYLES_DIR.mkdir(parents=True, exist_ok=True)
    styles = list_styles(args.category) if args.category else list_styles()

    ok = 0
    fail: list[str] = []
    for s in styles:
        sid = s["id"]
        if not args.force and is_style_local(sid):
            continue
        path = fetch_style(sid)
        if path:
            ok += 1
            print(f"✓ {sid}")
        else:
            fail.append(sid)
            print(f"✗ {sid}", file=sys.stderr)

    print(f"\nFetched: {ok}  Failed: {len(fail)}  Total in manifest: {len(styles)}")
    if fail:
        print("\nFailures (the ID may be wrong in our manifest, or network is blocked):", file=sys.stderr)
        for f in fail:
            print(f"  - {f}", file=sys.stderr)

    if get_locale(args.locale):
        print(f"\n✓ Locale: {args.locale}")
    else:
        print(f"\n✗ Locale fetch failed: {args.locale}", file=sys.stderr)

    return 0 if not fail else 1


if __name__ == "__main__":
    sys.exit(main())
