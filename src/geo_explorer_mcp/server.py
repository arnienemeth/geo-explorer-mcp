import os
import re
import unicodedata
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastmcp import FastMCP

# A client such as Claude Desktop launches the server from an arbitrary working
# directory, so a bare load_dotenv() would miss the project's .env and silently
# fall back to the public demo key. Look beside this module first, then walk up
# to the project root. An installed copy has no .env at all and reads the real
# environment, which is what a packaged install should do.
def _load_env() -> None:
    here = Path(__file__).resolve()
    for candidate in (here.with_name(".env"),
                      *(parent / ".env" for parent in here.parents[:4])):
        if candidate.is_file():
            load_dotenv(candidate)
            return
    load_dotenv()


_load_env()

mcp = FastMCP("Geo Explorer")

API_BASE = "https://api.restcountries.com/countries/v5"
API_KEY = os.getenv("RESTCOUNTRIES_API_KEY", "rc_live_demo")

FIELDS = ",".join([
    "names.common", "names.official", "capitals", "population",
    "area.kilometers", "region", "subregion", "continents", "landlocked",
    "currencies", "languages", "borders", "coordinates.lat",
    "coordinates.lng", "codes.alpha_3", "flag.url_png", "links.wikipedia",
])


def _capitals(value):
    """v5 returns a list of {name, coordinates, attributes}."""
    if not isinstance(value, list):
        return []
    return [
        {
            "name": c.get("name"),
            "latitude": c.get("coordinates", {}).get("lat"),
            "longitude": c.get("coordinates", {}).get("lng"),
            "is_primary": c.get("attributes", {}).get("primary"),
        }
        for c in value if isinstance(c, dict)
    ]


def _currencies(value):
    """v5 returns a list of {code, name, symbol}."""
    if not isinstance(value, list):
        return []
    return [
        {"code": v.get("code"), "name": v.get("name"), "symbol": v.get("symbol")}
        for v in value if isinstance(v, dict)
    ]


def _languages(value):
    """v5 returns a list of language objects with English and native names."""
    if not isinstance(value, list):
        return []
    return [
        {"name": v.get("name"), "native_name": v.get("native_name")}
        for v in value if isinstance(v, dict)
    ]


def _quality_note(regions):
    """Warn when upstream region names look damaged (truncated or mangled).

    Only the name attributes are affected; the geometry is still valid."""
    names = [r.get("name") or "" for r in regions]
    if any("*" in n for n in names):
        return (
            "Some region names appear truncated or corrupted in the upstream "
            "source data. The geometry is unaffected. Treat these labels as "
            "approximate and prefer local sources for official names."
        )
    return None


@mcp.tool()
async def get_country_profile(country: str) -> dict:
    """Get a factual profile of a country: capital cities with coordinates,
    population, area, region, currencies, languages (English and native
    names), bordering countries, flag and a Wikipedia link. Accepts a full
    or partial country name in any language, e.g. 'Slovakia',
    'Deutschland' or 'ger'."""

    url = f"{API_BASE}/name"
    params = {"q": country, "response_fields": FIELDS, "limit": 5}
    headers = {"Authorization": f"Bearer {API_KEY}"}

    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(url, params=params, headers=headers)

    if response.status_code == 401:
        return {"error": "Invalid or missing API key. Check your .env file."}
    if response.status_code == 429:
        return {"error": "Rate limited. Wait a few seconds and try again."}

    response.raise_for_status()
    payload = response.json()
    objects = payload.get("data", {}).get("objects", [])

    if not objects:
        return {"error": f"No country found matching '{country}'."}

    data = objects[0]

    return {
        "name": data.get("names", {}).get("common"),
        "official_name": data.get("names", {}).get("official"),
        "capitals": _capitals(data.get("capitals")),
        "population": data.get("population"),
        "area_km2": data.get("area", {}).get("kilometers"),
        "region": data.get("region"),
        "subregion": data.get("subregion"),
        "continents": data.get("continents", []),
        "landlocked": data.get("landlocked"),
        "currencies": _currencies(data.get("currencies")),
        "languages": _languages(data.get("languages")),
        "borders": data.get("borders", []),
        "latitude": data.get("coordinates", {}).get("lat"),
        "longitude": data.get("coordinates", {}).get("lng"),
        "country_code": data.get("codes", {}).get("alpha_3"),
        "flag_image": data.get("flag", {}).get("url_png"),
        "wikipedia": data.get("links", {}).get("wikipedia"),
        "other_matches": [
            o.get("names", {}).get("common") for o in objects[1:]
        ],
    }

GEOB_BASE = "https://www.geoboundaries.org/api/current/gbOpen"

_geojson_cache: dict[str, tuple] = {}


def _fold(text: str) -> str:
    """Lowercase, strip diacritics and punctuation: 'Magyarorszag' == 'Magyarorszag'."""
    folded = unicodedata.normalize("NFKD", text or "")
    folded = folded.encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]", "", folded)


_CODE_FIELDS = "codes.alpha_2,codes.alpha_3,names.common"

# Folded name -> country codes. Built once, on the first lookup that needs it.
_country_index: dict[str, dict] = {}


def _entry(obj: dict) -> dict:
    codes = obj.get("codes", {})
    return {
        "alpha_2": codes.get("alpha_2"),
        "alpha_3": codes.get("alpha_3"),
        "name": obj.get("names", {}).get("common"),
    }


async def _build_country_index(client: httpx.AsyncClient) -> None:
    """Index every country under a diacritic-folded key.

    REST Countries matches case-insensitively but NOT accent-insensitively,
    so 'Magyarorszag' misses where 'Magyarorszag' hits. Folding locally lets
    someone without a Hungarian keyboard still find Hungary.
    """
    if _country_index:
        return
    for offset in (0, 100, 200):
        r = await client.get(
            API_BASE,
            params={
                "limit": 100,
                "offset": offset,
                "response_fields": (
                    "codes.alpha_2,codes.alpha_3,names.common,"
                    "names.official,names.native,names.alternates"
                ),
            },
            headers={"Authorization": f"Bearer {API_KEY}"},
        )
        if r.status_code != 200:
            return
        objects = r.json().get("data", {}).get("objects", [])
        if not objects:
            break
        for obj in objects:
            names = obj.get("names", {})
            variants = [names.get("common"), names.get("official")]
            variants += names.get("alternates") or []
            native = names.get("native")
            if isinstance(native, dict):
                for forms in native.values():
                    if isinstance(forms, dict):
                        variants += [forms.get("common"), forms.get("official")]
            for variant in variants:
                key = _fold(variant or "")
                if key:
                    _country_index.setdefault(key, _entry(obj))


async def _resolve_codes(client: httpx.AsyncClient, country: str) -> dict:
    """Turn a country name in any language into its ISO alpha-2/alpha-3 codes.

    Tried in order, stopping at the first hit:
      1. the /name aggregate - common, official, alternate and native names;
      2. /names.translations - so 'Ungarn' and 'Hongrie' resolve too, which
         the aggregate deliberately excludes to avoid false positives;
      3. a locally built diacritic-folded index, so 'Magyarorszag' works
         for someone who cannot type 'Magyarorszag'.
    """
    headers = {"Authorization": f"Bearer {API_KEY}"}

    for path in (f"{API_BASE}/name", f"{API_BASE}/names.translations"):
        r = await client.get(
            path,
            params={"q": country, "response_fields": _CODE_FIELDS, "limit": 1},
            headers=headers,
        )
        if r.status_code != 200:
            continue
        objects = r.json().get("data", {}).get("objects", [])
        if objects:
            return _entry(objects[0])

    await _build_country_index(client)
    return _country_index.get(_fold(country), {})


async def _resolve_iso3(client: httpx.AsyncClient, country: str) -> str | None:
    """Backwards-compatible alpha-3 lookup."""
    return (await _resolve_codes(client, country)).get("alpha_3")


async def _region_names(client: httpx.AsyncClient, url: str) -> tuple:
    """Download the simplified GeoJSON once; return (region names, final URL).

    geoBoundaries publishes a github.com/.../raw/... link, but the files are
    stored in Git LFS, so that link 302-redirects to media.githubusercontent.com.
    The redirect hop carries an EMPTY access-control-allow-origin header, and a
    browser enforces CORS on every hop of a redirect -- so fetching the original
    URL from a web page fails with "TypeError: Failed to fetch", even though the
    final response sends "access-control-allow-origin: *".

    Resolving the redirect here means callers get a URL a browser can actually
    fetch. Do not substitute raw.githubusercontent.com: that returns a 131-byte
    Git LFS pointer file instead of geometry, and the map renders blank.
    """
    if url in _geojson_cache:
        return _geojson_cache[url]
    r = await client.get(url, follow_redirects=True)
    r.raise_for_status()
    features = r.json().get("features", [])
    names = [
        {
            "name": f.get("properties", {}).get("shapeName"),
            "id": f.get("properties", {}).get("shapeID"),
        }
        for f in features
    ]
    result = (names, str(r.url))
    _geojson_cache[url] = result
    return result


@mcp.tool()
async def get_map_data(country: str, level: str = "ADM1") -> dict:
    """Get administrative boundary data for a country, for drawing an
    interactive map. Returns the names of each sub-national region plus a
    URL to simplified GeoJSON that a map library (Leaflet, D3, Mapbox) can
    load directly in the browser. Use level 'ADM0' for the country outline,
    'ADM1' for states/provinces/regions (e.g. Slovak kraje, German
    Bundeslander), or 'ADM2' for counties/districts. Not every country has
    ADM2 data."""

    level = level.upper()
    if level not in {"ADM0", "ADM1", "ADM2", "ADM3"}:
        return {"error": f"Unsupported level '{level}'. Use ADM0, ADM1 or ADM2."}

    async with httpx.AsyncClient(timeout=30.0) as client:
        iso3 = await _resolve_iso3(client, country)
        if not iso3:
            return {"error": f"Could not identify a country from '{country}'."}

        r = await client.get(f"{GEOB_BASE}/{iso3}/{level}/", follow_redirects=True)

        if r.status_code == 404 or not r.text.strip():
            return {
                "error": f"No {level} boundaries published for {iso3}.",
                "hint": "Try ADM1, which exists for every country.",
            }

        r.raise_for_status()
        meta = r.json()
        if isinstance(meta, list):
            meta = meta[0] if meta else {}

        geojson_url = meta.get("simplifiedGeometryGeoJSON")

        regions, fetchable_url = [], None
        if geojson_url:
            try:
                regions, fetchable_url = await _region_names(client, geojson_url)
            except Exception:
                pass

    return {
        "country": meta.get("boundaryName"),
        "iso3": meta.get("boundaryISO"),
        "level": level,
        "region_count": meta.get("admUnitCount"),
        "regions": regions,
        "data_quality_note": _quality_note(regions),
        "geojson_url": fetchable_url or geojson_url,
        "geojson_url_note": (
            "Fetch this URL directly. It is the resolved Git LFS media URL "
            "and sends access-control-allow-origin: *, so a browser can load "
            "it cross-origin. Do not rewrite it to raw.githubusercontent.com "
            "(returns an LFS pointer, not geometry) or to github.com/.../raw/ "
            "(the redirect hop fails the browser CORS check)."
        ),
        "preview_image": meta.get("imagePreview"),
        "year_represented": meta.get("boundaryYearRepresented"),
        "source": meta.get("boundarySource"),
        "license": meta.get("boundaryLicense"),
        "attribution": (
            f"Boundaries from geoBoundaries (geoboundaries.org). "
            f"Source: {meta.get('boundarySource')}. "
            f"Licence: {meta.get('boundaryLicense')}."
        ),
    }
    

# --------------------------------------------------------------------------
# Wikidata: per-region statistics
# --------------------------------------------------------------------------

WIKIDATA_ENDPOINT = "https://query.wikidata.org/sparql"

# Wikimedia's User-Agent policy requires a descriptive agent WITH a contact
# (a URL or email). A generic or contactless UA is answered with HTTP 403.
# Override the contact with the GEO_EXPLORER_CONTACT environment variable.
WIKIDATA_CONTACT = os.getenv(
    "GEO_EXPLORER_CONTACT", "https://github.com/topics/model-context-protocol")
WIKIDATA_UA = f"geo-explorer-mcp/0.1 ({WIKIDATA_CONTACT}) python-httpx"

_wikidata_cache: dict[str, list] = {}

# P297 ISO 3166-1 alpha-2 (country) | P300 ISO 3166-2 (subdivision)
# P1082 population | P2046 area | P1705 native label | P36 capital | P625 coords
# Filtering on the ISO 3166-2 prefix is the country filter -- those codes are
# defined as <alpha-2>-<subdivision>. Joining through ?i wdt:P17 ?country
# instead makes Wikidata scan everything in the country and TIMES OUT (504)
# for large ones: the USA took >60s that way and 0.9s this way.
#
# NOTE: no '#' comments inside this query. It is collapsed to a single line
# before sending, and in SPARQL '#' comments out the rest of the LINE -- which
# after collapsing is the whole query. (Symptom: HTTP 400 from Wikidata.)
#
# Area uses p:P2046/psn:P2046, the NORMALISED value in SI base units (square
# metres). The plain wdt:P2046 is whatever unit an editor typed -- Wikidata
# mixes km2, hectares and m2 -- so reading it raw is wrong by up to 1e6.
_SPARQL_REGIONS = """
SELECT ?iso ?iLabel ?native ?pop ?areaM2 ?i ?typeLabel ?capLabel ?capPop ?capCoord ?wiki WHERE {
  ?i wdt:P300 ?iso .
  FILTER(STRSTARTS(?iso, "__A2__-"))
  OPTIONAL { ?i wdt:P1082 ?pop }
  OPTIONAL { ?i p:P2046/psn:P2046 [ wikibase:quantityAmount ?areaM2 ] }
  OPTIONAL { ?i wdt:P1705 ?native }
  OPTIONAL { ?i wdt:P31 ?type }
  OPTIONAL { ?i wdt:P36 ?cap .
             OPTIONAL { ?cap wdt:P1082 ?capPop }
             OPTIONAL { ?cap wdt:P625 ?capCoord } }
  OPTIONAL { ?wiki schema:about ?i ; schema:isPartOf <https://en.wikipedia.org/> }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en,__LANG__" }
} ORDER BY ?iso
"""


def _match_key(name: str) -> str:
    """Normalise a region name so geoBoundaries and Wikidata spellings join.

    The two sources use different conventions for the same place:
    "Region of Banska Bystrica" vs "Banska Bystrica Region"; "Bayern" vs
    "Bavaria" (matched via the native name); "Commonwealth of the Northern
    Mariana Islands" vs "Northern Mariana Islands". Folding diacritics,
    punctuation and the wrapper words collapses them onto one key.
    """
    if not name:
        return ""
    folded = unicodedata.normalize("NFKD", name)
    folded = folded.encode("ascii", "ignore").decode().lower()
    folded = re.sub(r"[^a-z0-9\s]", " ", folded)
    folded = re.sub(
        r"\b(the|of|region|province|state|county|district|kraj|freistaat|"
        r"free state|commonwealth|united states|us|u s|self-governing|"
        r"metropolitan|autonomous|land|bundesland)\b",
        " ", folded)
    return re.sub(r"[^a-z0-9]", "", folded)


def _point(value: str):
    """Parse a WKT 'Point(lng lat)' literal into (lat, lng)."""
    if not value:
        return None, None
    m = re.search(r"Point\(([-\d.]+)\s+([-\d.]+)\)", value)
    if not m:
        return None, None
    return float(m.group(2)), float(m.group(1))


@mcp.tool()
async def get_region_details(country: str) -> dict:
    """Get population, area, capital city and reference links for each
    first-level region (ADM1) of a country, from Wikidata.

    Complements get_map_data, which supplies the geometry but carries no
    statistics. Join the two on the 'match_key' field, which normalises the
    different naming conventions the two sources use.

    Each region returns its ISO 3166-2 code, English and native names,
    population, area in km2, its capital city (with that city's own
    population and coordinates), and Wikidata plus Wikipedia URLs for
    further reading. Coverage is best for ADM1; many countries do not
    publish ISO 3166-2 codes below that level."""

    async with httpx.AsyncClient(timeout=45.0) as client:
        codes = await _resolve_codes(client, country)
        alpha2 = codes.get("alpha_2")
        if not alpha2:
            return {"error": f"Could not identify a country from '{country}'."}

        if alpha2 in _wikidata_cache:
            regions = _wikidata_cache[alpha2]
        else:
            query = " ".join((_SPARQL_REGIONS
                              .replace("__A2__", alpha2)
                              .replace("__LANG__", alpha2.lower())).split())
            try:
                r = await client.get(
                    WIKIDATA_ENDPOINT,
                    params={"query": query, "format": "json"},
                    headers={"Accept": "application/sparql-results+json",
                             "User-Agent": WIKIDATA_UA},
                    follow_redirects=True,
                )
            except Exception as exc:
                return {"error": f"Could not reach Wikidata: {exc}"}

            if r.status_code == 429:
                return {"error": "Wikidata rate limited the request. Retry shortly."}
            if r.status_code != 200:
                return {
                    "error": f"Wikidata returned HTTP {r.status_code}.",
                    "sent_user_agent": WIKIDATA_UA,
                    "response_body": " ".join(r.text.split())[:300],
                    "hint": (
                        "HTTP 403 usually means the User-Agent was rejected. "
                        "Wikimedia requires a descriptive agent with a contact "
                        "URL or email; set GEO_EXPLORER_CONTACT in your .env."
                    ),
                }

            rows = r.json().get("results", {}).get("bindings", [])

            # A region with several native labels (bilingual areas publish one
            # per language) produces one row each. Merge them back together.
            merged: dict[str, dict] = {}
            for row in rows:
                g = lambda k: row.get(k, {}).get("value")
                iso = g("iso")
                if not iso:
                    continue
                entry = merged.get(iso)
                if entry is None:
                    lat, lng = _point(g("capCoord"))
                    item = g("i") or ""
                    entry = {
                        "iso_3166_2": iso,
                        "name": g("iLabel"),
                        "native_names": [],
                        "population": int(float(g("pop"))) if g("pop") else None,
                        "area_km2": (round(float(g("areaM2")) / 1e6, 1)
                                     if g("areaM2") else None),
                        "subdivision_types": [],
                        "capital": {
                            "name": g("capLabel"),
                            "population": int(float(g("capPop"))) if g("capPop") else None,
                            "latitude": lat,
                            "longitude": lng,
                        } if g("capLabel") else None,
                        "wikidata_url": item.replace(
                            "http://www.wikidata.org/entity/",
                            "https://www.wikidata.org/wiki/") or None,
                        "wikipedia_url": g("wiki"),
                        "match_key": _match_key(g("iLabel") or ""),
                        "match_keys": [],
                    }
                    merged[iso] = entry
                native = g("native")
                if native and native not in entry["native_names"]:
                    entry["native_names"].append(native)
                for variant in (g("iLabel"), native):
                    vkey = _match_key(variant or "")
                    if vkey and vkey not in entry["match_keys"]:
                        entry["match_keys"].append(vkey)
                kind = g("typeLabel")
                if kind and kind not in entry["subdivision_types"]:
                    entry["subdivision_types"].append(kind)

            regions = sorted(merged.values(), key=lambda e: e["iso_3166_2"])
            _wikidata_cache[alpha2] = regions

    if not regions:
        return {
            "error": f"Wikidata has no ISO 3166-2 subdivisions for {alpha2}.",
            "hint": "Coverage is best for ADM1 in countries that publish ISO 3166-2 codes.",
        }

    seen: dict[str, int] = {}
    for reg in regions:
        seen[reg["match_key"]] = seen.get(reg["match_key"], 0) + 1
    collisions = sorted(k for k, n in seen.items() if n > 1)

    notes = []
    if collisions:
        notes.append(
            "These match_key values are ambiguous because more than one "
            f"subdivision normalises to them: {', '.join(collisions)}. "
            "Disambiguate with iso_3166_2 or subdivision_types."
        )
    # Count REGIONS per type, not raw statements. Wikidata also records
    # historical types -- Puerto Rico is still a "Provincial deputation in
    # Spanish America" -- so the bare list of distinct labels is noise. What
    # matters is whether one kind dominates: 50 of the 56 US entries are
    # states, while Hungary splits between counties and cities that sit
    # inside them.
    counts: dict[str, int] = {}
    for reg in regions:
        for kind in reg["subdivision_types"]:
            counts[kind] = counts.get(kind, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    type_summary = [{"type": k, "regions": n} for k, n in ranked]

    dominant = ranked[0][1] / len(regions) if ranked and regions else 1.0
    if dominant < 0.75:
        top = ", ".join(f"{k} ({n})" for k, n in ranked[:3])
        notes.append(
            "No single kind of subdivision dominates this country's ISO 3166-2 "
            f"list -- the most common are {top}. Entries of different kinds "
            "often overlap geographically (a city inside a province), so their "
            "areas and populations must not be summed, and there will be fewer "
            "boundary shapes than entries here. Filter on subdivision_types to "
            "compare like with like."
        )

    return {
        "country": codes.get("name"),
        "iso2": alpha2,
        "region_count": len(regions),
        "regions": regions,
        "subdivision_type_summary": type_summary,
        "data_quality_notes": notes or None,
        "join_hint": (
            "Normalise get_map_data's region names the same way -- lowercase, "
            "strip diacritics, punctuation and wrapper words such as 'region', "
            "'state', 'freistaat', 'commonwealth', 'the', 'of' -- then look the "
            "result up in 'match_keys'. Use 'match_keys' (every name variant, "
            "including native ones) rather than 'match_key' (English label only): "
            "geoBoundaries calls it 'Bayern' where Wikidata's English label is "
            "'Bavaria', and only the native name bridges those."
        ),
        "attribution": ("Region statistics from Wikidata (wikidata.org), CC0 1.0. "
                        "Areas are Wikidata normalised values converted from m2 to km2."),
    }

def main() -> None:
    """Console-script entry point: run the server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()