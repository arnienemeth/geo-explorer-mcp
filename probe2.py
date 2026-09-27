"""Call the MCP tools directly, the way a client would.

Runs against the real server object in-process: no Inspector, no browser,
no Claude Desktop. This is what proves the Python tools work, as opposed
to the demo page, which reimplements the same idea in JavaScript.

    uv run python probe2.py
"""

import asyncio
import re
import unicodedata

from fastmcp import Client

from server import mcp

# geoBoundaries ADM1 counts, verified against the published geometry.
COUNTRIES = [
    ("Slovakia", 8),
    ("Germany", 16),
    ("United Kingdom", 4),
    ("United States", 56),
]


def key(name: str) -> str:
    """Mirror of the server's _match_key, for checking the join independently."""
    f = unicodedata.normalize("NFKD", name or "")
    f = f.encode("ascii", "ignore").decode().lower()
    f = re.sub(r"[^a-z0-9\s]", " ", f)
    f = re.sub(r"\b(the|of|region|province|state|county|district|kraj|freistaat|"
               r"free state|commonwealth|united states|us|u s|self-governing|"
               r"metropolitan|autonomous|land|bundesland)\b", " ", f)
    return re.sub(r"[^a-z0-9]", "", f)


async def main():
    failures = []

    async with Client(mcp) as client:
        tools = [t.name for t in await client.list_tools()]
        print("tools advertised:", tools)
        assert len(tools) == 3, tools

        print("\n=== get_country_profile ===")
        d = (await client.call_tool("get_country_profile", {"country": "Slovensko"})).data
        print(f"  'Slovensko' -> {d['name']} | pop {d['population']:,} | "
              f"{d['capitals'][0]['name']} | {d['languages'][0]['native_name']}")

        print("\n=== get_map_data + get_region_details, joined ===")
        for name, expected in COUNTRIES:
            m = (await client.call_tool(
                "get_map_data", {"country": name, "level": "ADM1"})).data
            if "error" in m:
                failures.append(f"{name}: map_data {m['error']}")
                print(f"  {name:16} FAILED: {m['error']}")
                continue

            assert "media.githubusercontent.com" in m["geojson_url"], \
                f"{name}: geojson_url must be the resolved Git LFS media URL"

            w = (await client.call_tool("get_region_details", {"country": name})).data
            if "error" in w:
                failures.append(f"{name}: region_details {w['error']}")
                print(f"  {name:16} FAILED: {w['error']}")
                continue

            # Join every boundary name against every Wikidata name variant.
            index = set()
            for r in w["regions"]:
                index.update(r.get("match_keys") or [r["match_key"]])
            names = [r["name"] for r in m["regions"]]
            hit = sum(1 for n in names if key(n) in index)

            flag = "" if len(names) == expected else f"  (expected {expected})"
            print(f"  {name:16} boundaries={len(names):3}  wikidata={w['region_count']:3}  "
                  f"joined={hit:3}/{len(names)}{flag}")

            if len(names) != expected:
                failures.append(f"{name}: got {len(names)} regions, expected {expected}")
            if hit < len(names) * 0.9:
                failures.append(f"{name}: only {hit}/{len(names)} joined")
            for note in (w.get("data_quality_notes") or []):
                print(f"      note: {note[:96]}...")

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S):")
        for f in failures:
            print("  -", f)
        raise SystemExit(1)
    print("all checks passed")


asyncio.run(main())
