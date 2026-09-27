"""Check which country spellings the MCP server can resolve.

Calls the server's own resolver, not the raw API -- so this measures what
a user of the tool actually gets, through the full fallback chain:
  1. /name aggregate   (common, official, alternates, native)
  2. /names.translations  (Ungarn, Hongrie, ...)
  3. local diacritic-folded index  (Magyarorszag)

    uv run python probe_names.py
"""

import asyncio

import httpx

# Reaches into an internal, so it imports the module directly rather than
# through the root shim, which only re-exports the public surface.
from geo_explorer_mcp.server import _resolve_codes

CASES = [
    ("Hungary",       "English common name",            "HU"),
    ("Magyarország",  "Hungarian native name",          "HU"),
    ("Magyarorszag",  "native name, diacritics dropped", "HU"),
    ("magyarország",  "native name, lowercase",         "HU"),
    ("Ungarn",        "German translation",             "HU"),
    ("Hongrie",       "French translation",             "HU"),
    ("Deutschland",   "German native name",             "DE"),
    ("Slovensko",     "Slovak native name",             "SK"),
    ("Cote d'Ivoire", "accents dropped, apostrophe",    "CI"),
    ("Zblorkistan",   "nonsense, should miss",          None),
]


async def main():
    ok = miss = wrong = 0
    async with httpx.AsyncClient(timeout=30.0) as client:
        for term, why, expect in CASES:
            got = await _resolve_codes(client, term)
            a2 = got.get("alpha_2")
            if expect is None:
                mark = "OK  " if not a2 else "BAD "
                ok += not a2
                wrong += bool(a2)
            elif a2 == expect:
                mark, _ = "OK  ", None
                ok += 1
            elif a2 is None:
                mark = "MISS"
                miss += 1
            else:
                mark = "WRONG"
                wrong += 1
            print(f"{mark} {term:16} -> {str(a2):5} {str(got.get('name')):16} {why}")
    print(f"\n{ok} ok, {miss} missed, {wrong} wrong")


asyncio.run(main())
