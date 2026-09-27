"""Call the MCP tools directly, the way a client would.

Runs against the real server object in-process: no Inspector, no browser,
no Claude Desktop. This is what proves the Python tools work, as opposed
to the demo page, which reimplements the same idea in JavaScript.

    uv run python probe2.py
"""

import asyncio
import json

from fastmcp import Client

from server import mcp


async def call(client, tool, args):
    print(f"\n{'=' * 62}\n{tool}({args})\n{'=' * 62}")
    try:
        result = await client.call_tool(tool, args)
        return result.data
    except Exception as exc:
        print(f"FAILED: {type(exc).__name__}: {exc}")
        return None


async def main():
    async with Client(mcp) as client:
        tools = await client.list_tools()
        print("tools advertised:", [t.name for t in tools])

        # 1. Country profile
        d = await call(client, "get_country_profile", {"country": "Slovakia"})
        if d:
            print(f"  {d['name']} | pop {d['population']:,} | {d['area_km2']:,} km2")
            print(f"  capital: {d['capitals'][0]['name']} "
                  f"({d['capitals'][0]['latitude']}, {d['capitals'][0]['longitude']})")
            print(f"  language: {d['languages'][0]['native_name']}")

        # 2. Boundaries
        d = await call(client, "get_map_data", {"country": "Slovakia", "level": "ADM1"})
        if d:
            print(f"  {d['region_count']} regions, licence: {d['license']}")
            print(f"  geojson_url host: {d['geojson_url'].split('/')[2]}")
            assert "media.githubusercontent.com" in d["geojson_url"], \
                "geojson_url must be the resolved LFS media URL"
            print("  OK: url is browser-fetchable")
            print(f"  quality note: {d['data_quality_note']}")

        # 3. Wikidata region statistics  <-- the new one
        d = await call(client, "get_region_details", {"country": "Slovakia"})
        if d and "error" not in d:
            print(f"  {d['region_count']} regions from Wikidata")
            for r in d["regions"]:
                cap = r["capital"] or {}
                print(f"  {r['iso_3166_2']}  {r['name'][:22]:22} "
                      f"pop={str(r['population']):>8}  area={str(r['area_km2']):>8}  "
                      f"cap={cap.get('name', '-')} ({cap.get('population')})")
                print(f"        native={r['native_names']}  key={r['match_key']}")
            print(f"\n  wikipedia sample: {d['regions'][0]['wikipedia_url']}")
            print(f"  wikidata  sample: {d['regions'][0]['wikidata_url']}")
        elif d:
            print("  ", d)

        # 4. The join the whole design depends on
        m = await call(client, "get_map_data", {"country": "Slovakia", "level": "ADM1"})
        w = await call(client, "get_region_details", {"country": "Slovakia"})
        if m and w and "error" not in w:
            import re
            import unicodedata

            def key(n):
                f = unicodedata.normalize("NFKD", n or "")
                f = f.encode("ascii", "ignore").decode().lower()
                f = re.sub(r"\b(region of|region|kraj)\b", " ", f)
                return re.sub(r"[^a-z0-9]", "", f)

            gb = {key(r["name"]) for r in m["regions"]}
            wd = {r["match_key"] for r in w["regions"]}
            print(f"\n  JOIN: {len(gb & wd)} of {len(gb)} boundary regions matched")
            print(f"  unmatched: {(gb ^ wd) or 'none'}")


asyncio.run(main())
