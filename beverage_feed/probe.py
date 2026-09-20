"""Manual Tesco category-walk reconnaissance probe (ff-20).

**Manual only, CI egress only.** This probe exists to learn the *shape* of
Tesco Ireland's aisle enumeration before any adapter is written, per
``research/tesco-ie-enumeration-2026-09-20.md``: third-party sources
(``basketeer``, Apify actors) claim a ``browseCategory`` operation exists on
the storefront GraphQL gateway and that category listing pages can be walked,
but nobody publishes the response shape, the pagination semantics, or an IE
confirmation. This probe collects that evidence and stops.

Rules it follows (and must keep following):

- It is run from GitHub Actions (``probe.yml``), never from the home IP —
  Tesco's Akamai edge 403s this network (see ``docs/retailers.md``).
- It makes a handful of requests at most, spaced, through the shared
  ``RetailerTransport`` (Chrome-impersonated when ``curl-cffi`` is present).
- It never writes to the feed database, never approves mappings, never
  records a Price Observation. The artifact is evidence, not data.
- It does not guess: if the schema does not offer a category operation, or a
  required argument has no known value, that is recorded as the finding.

Output: an artifact directory with ``probe-summary.json`` (every attempt with
status, sizes, and scrubbed samples), ``introspection-fields.json`` (the
gateway's query field names), and ``category-page-N.json`` (per-page
structural summary). Raw HTML is never dumped whole — a bounded sample and
the embedded JSON blob keys only.

## What the first run already established (2026-09-20, run 35543767493)

- **The HTML storefront route is closed from CI**: both Drinks aisle URLs
  answered HTTP 200 with a 2712-byte Akamai Bot Manager interstitial
  (``sec-if-cpt-container`` / "Powered and protected by Akamai"), no title,
  no product data. Category-page scraping is therefore not the path for
  Tesco IE — unlike Lidl's range pages, which do serve data.
- Introspection returned HTTP 400 on a single-operation request body.

So this module now (1) sends the **batch array** shape the working collection
path uses, (2) carries a **control operation** — the production
``GetProductByTpnb`` query — so "gateway/apikey broken" and "this operation
rejected" are distinguishable, and (3) uses **error elicitation**: argless
queries against candidate category fields, whose error text names the real
arguments when a field exists. No argument *values* are ever invented.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from . import source_http
from .collector import (
    TESCO_GRAPHQL_BATCH_LIMIT,
    TESCO_PRODUCT_QUERY,
    safe_record,
)

TESCO_GRAPHQL_ENDPOINT = "https://xapi.tesco.com/"
TESCO_SHOP_LANDING = "https://www.tesco.ie/groceries/"
TESCO_GRAPHQL_ORIGIN = "https://www.tesco.ie"

#: Best-effort aisle URLs. A 404 here is itself a finding (the locale or path
#: segment is wrong) and is recorded verbatim.
DEFAULT_CATEGORY_URLS: tuple[str, ...] = (
    "https://www.tesco.ie/groceries/en-IE/shop/drinks/fizzy-drinks/all",
    "https://www.tesco.ie/groceries/en-IE/shop/drinks/all",
)

INTROSPECTION_QUERY = """
query CategoryWalkIntrospection {
  __schema {
    queryType {
      fields {
        name
        args { name type { kind name ofType { kind name ofType { kind name } } } }
      }
    }
  }
}
""".strip()

_CATEGORY_FIELD_RE = re.compile(r"categor|browse|aisle|shelf|listing|grid", re.I)

#: Field names worth eliciting an error from. An argless query tells us, from
#: the gateway's own error text, whether the field exists and which arguments
#: it requires — no invented values, no guessed shapes.
CATEGORY_CANDIDATE_FIELDS: tuple[str, ...] = (
    "browseCategory",
    "category",
    "productList",
)

#: Top-level fields that answered on the IE gateway (2026-09-20 run
#: 35544010364) and therefore carry the scans below.
CATEGORY_TARGET_FIELDS: tuple[str, ...] = ("category", "productList")

#: Argument names to test on a target field. An "Unknown argument" error
#: means the name is wrong; any other answer means the argument exists — and a
#: type error names the type it expects.
CATEGORY_ARG_CANDIDATES: tuple[str, ...] = (
    "id",
    "categoryId",
    "categoryKey",
    "node",
    "nodeId",
    "path",
    "key",
    "slug",
    "url",
)

#: Sub-field names to test on the returned list type. Unknown names come back
#: as validation errors naming the offending field, so the valid ones are the
#: complement of the error set.
CATEGORY_SUBFIELD_CANDIDATES: tuple[str, ...] = (
    "id",
    "name",
    "title",
    "total",
    "count",
    "products",
    "items",
    "nodes",
    "page",
    "pagination",
)

_UNKNOWN_ARG_MARKER = "unknown argument"
_DRINK_LINK_RE = re.compile(
    r"href=\"(?P<url>[^\"]*(?:drinks?|fizzy|soft-drinks?)[^\"]*)\"", re.I
)
_JSON_BLOB_RE = re.compile(
    r"<script[^>]*type=\"application/(?:json|ld\+json)\"[^>]*>(?P<body>.*?)</script>",
    re.S | re.I,
)
_CATEGORY_VALUE_RE = re.compile(r"(?:category|node|shelf)[A-Za-z]*[\"']?\s*[:=]\s*[\"']?(?P<value>[A-Za-z0-9/_.-]{1,64})")
_HTML_SAMPLE_CHARS = 4000
_MAX_INLINE_PAYLOAD_CHARS = 200_000

#: A real Tesco TPNB for the batch control operation (curated mappings).
MAPPING_PATH = Path("data") / "mappings.json"


def _graphql_batch_request(
    operations: Sequence[Mapping[str, Any]], api_key: str
) -> urllib.request.Request:
    """POST the batch-array shape the working collection path uses."""
    return urllib.request.Request(
        TESCO_GRAPHQL_ENDPOINT,
        data=json.dumps(list(operations)).encode(),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "drinks-tracker/0.1",
            "x-apikey": api_key,
            "region": "IE",
            "language": "en-IE",
            "origin": TESCO_GRAPHQL_ORIGIN,
            "referer": TESCO_GRAPHQL_ORIGIN + "/",
        },
        method="POST",
    )


def _sole_result(payload: Any) -> Any:
    """First element of a batch reply (introspection sends one operation)."""
    if isinstance(payload, list) and payload:
        return payload[0]
    return payload


def _first_tesco_tpnb(path: Path) -> str | None:
    """A real TPNB from the curated mappings — the batch's control value."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    entries = data.get("tesco") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if isinstance(entry, Mapping) and entry.get("source_tpnb"):
            return str(entry["source_tpnb"])
    return None


def _scan_operations() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(shape batch, argument batch) — both evidence-only, no values invented.

    Shape batch: one argless existence query per candidate field, plus one
    sub-field scan per known list field. Argument batch: one query per
    candidate argument name, with a deliberately ill-typed value so a valid
    argument answers with a type error naming the type it wants.
    """
    shape: list[dict[str, Any]] = [
        {
            "operationName": f"Probe{field[:1].upper()}{field[1:]}",
            "variables": {},
            "query": f"query Probe{field[:1].upper()}{field[1:]} {{ {field} {{ __typename }} }}",
        }
        for field in CATEGORY_CANDIDATE_FIELDS
    ]
    subfields = " ".join(CATEGORY_SUBFIELD_CANDIDATES)
    for field in CATEGORY_TARGET_FIELDS:
        name = f"SubfieldScan_{field}"
        shape.append(
            {
                "operationName": name,
                "variables": {},
                "query": f"query {name} {{ {field} {{ {subfields} }} }}",
            }
        )
    arguments: list[dict[str, Any]] = []
    for field in CATEGORY_TARGET_FIELDS:
        for argument in CATEGORY_ARG_CANDIDATES:
            name = f"ArgScan_{field}_{argument}"
            arguments.append(
                {
                    "operationName": name,
                    "variables": {},
                    "query": (
                        f'query {name} {{ {field}({argument}: "1") {{ __typename }} }}'
                    ),
                }
            )
    return shape, arguments


def classify_arg_scan(
    results: Sequence[Mapping[str, Any]], field: str
) -> dict[str, Any]:
    """Split an argument scan into accepted names and names the gateway rejects."""
    accepted: list[dict[str, Any]] = []
    unknown: list[str] = []
    prefix = f"ArgScan_{field}_"
    for row in results:
        operation = str(row.get("operation") or "")
        if not operation.startswith(prefix):
            continue
        argument = operation[len(prefix):]
        detail = " ".join(str(item) for item in (row.get("errors") or []))
        if _UNKNOWN_ARG_MARKER in detail.lower():
            unknown.append(argument)
        else:
            accepted.append(
                {
                    "arg": argument,
                    "outcome": row.get("outcome"),
                    "detail": detail[:200],
                }
            )
    return {"field": field, "accepted": accepted, "unknown": unknown}


def classify_subfield_scan(
    row: Mapping[str, Any] | None,
    candidates: Sequence[str] = CATEGORY_SUBFIELD_CANDIDATES,
) -> dict[str, Any]:
    """Valid sub-fields are the candidates the gateway did not complain about."""
    if not row:
        return {"available": False}
    if row.get("outcome") == "answered":
        return {
            "available": True,
            "valid": list(candidates),
            "invalid": [],
            "data_keys": row.get("data_keys"),
        }
    detail = " ".join(str(item) for item in (row.get("errors") or []))
    invalid = [name for name in candidates if f'"{name}"' in detail]
    return {
        "available": True,
        "valid": [name for name in candidates if name not in invalid],
        "invalid": invalid,
    }


def _summarize_batch(
    operations: Sequence[Mapping[str, Any]], payload: Any
) -> list[dict[str, Any]]:
    """Per-operation outcome: the gateway's errors verbatim, or the reply shape."""
    results: list[dict[str, Any]] = []
    for index, operation in enumerate(operations):
        entry: dict[str, Any] = {"operation": str(operation.get("operationName"))}
        if not isinstance(payload, list):
            entry["outcome"] = "no batch reply"
            entry["reply_type"] = type(payload).__name__
            results.append(entry)
            continue
        reply = payload[index] if index < len(payload) else None
        if not isinstance(reply, Mapping):
            entry["outcome"] = "missing reply"
        elif reply.get("errors"):
            entry["outcome"] = "rejected"
            entry["errors"] = [
                str(item.get("message")) if isinstance(item, Mapping) else str(item)
                for item in (reply["errors"] or [])
            ][:4]
        else:
            data = reply.get("data")
            entry["outcome"] = "answered"
            entry["data_keys"] = (
                sorted(data.keys())[:20] if isinstance(data, Mapping) else None
            )
        results.append(entry)
    return results


class Transport(Protocol):
    """The subset of ``RetailerTransport`` this probe uses."""

    def send(
        self,
        request: urllib.request.Request,
        *,
        parse_json: bool = True,
        capture_errors: bool = False,
    ) -> Any:
        ...


def _clean_url(url: str) -> str:
    """URL with query values stripped (keys only) — no tokens in artifacts."""
    parsed = urllib.parse.urlsplit(url)
    keys = [key for key, _ in urllib.parse.parse_qsl(parsed.query)]
    query = "&".join(f"{key}=…" for key in keys)
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, query, "")
    )


def _attempt(
    transport: Transport,
    request: urllib.request.Request,
    *,
    parse_json: bool = True,
    capture_errors: bool = False,
) -> tuple[dict[str, Any], Any]:
    """Send one request, recording the outcome as artifact evidence."""
    record: dict[str, Any] = {
        "method": request.get_method(),
        "url": _clean_url(request.full_url),
    }
    try:
        payload = transport.send(
            request, parse_json=parse_json, capture_errors=capture_errors
        )
    except Exception as exc:  # evidence: the failure text is the finding
        record["outcome"] = "error"
        record["error"] = f"{type(exc).__name__}: {exc}"
        return record, None
    if isinstance(payload, dict) and payload.get("error_response"):
        # HTTP >= 400 with the retailer's own explanation body.
        record["outcome"] = "http_error"
        record["http_status"] = payload.get("status")
        record["retry_after"] = payload.get("retry_after")
        body = str(payload.get("body") or "")
        record["body"] = body[:source_http.ERROR_BODY_CHARS]
        return record, payload.get("json")
    record["outcome"] = "ok"
    if isinstance(payload, (bytes, bytearray)):
        record["bytes"] = len(payload)
        record["sample"] = payload[:_HTML_SAMPLE_CHARS].decode("utf-8", "replace")
    else:
        rendered = safe_record(payload) or ""
        record["chars"] = len(rendered)
        if len(rendered) <= _MAX_INLINE_PAYLOAD_CHARS:
            record["payload"] = rendered
        else:
            record["keys"] = sorted(payload.keys()) if isinstance(payload, dict) else None
    return record, payload


def _render_type(node: Any) -> str:
    """Render a GraphQL type reference from an introspection node."""
    if not isinstance(node, dict):
        return "?"
    kind = node.get("kind") or "?"
    name = node.get("name")
    if name:
        return str(name)
    inner = _render_type(node.get("ofType"))
    if kind == "NON_NULL":
        return f"{inner}!"
    if kind == "LIST":
        return f"[{inner}]"
    return f"{kind}<{inner}>"


def _graphql_request(query: str, api_key: str, variables: dict[str, Any] | None) -> urllib.request.Request:
    body: dict[str, Any] = {"query": query}
    if variables is not None:
        body["variables"] = variables
    return urllib.request.Request(
        TESCO_GRAPHQL_ENDPOINT,
        data=json.dumps(body).encode(),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "drinks-tracker/0.1",
            "x-apikey": api_key,
            "region": "IE",
            "language": "en-IE",
            "origin": TESCO_GRAPHQL_ORIGIN,
            "referer": TESCO_GRAPHQL_ORIGIN + "/",
        },
        method="POST",
    )


def summarize_introspection(payload: Any) -> dict[str, Any]:
    """Query fields from an introspection reply, with category-like args."""
    fields: list[str] = []
    category_like: dict[str, Any] = {}
    try:
        nodes = payload["data"]["__schema"]["queryType"]["fields"]
    except (KeyError, TypeError):
        return {"available": False, "reason": "no __schema.queryType.fields in reply"}
    for node in nodes or []:
        name = str(node.get("name"))
        fields.append(name)
        if _CATEGORY_FIELD_RE.search(name):
            category_like[name] = [
                {
                    "name": arg.get("name"),
                    "type": _render_type(arg.get("type")),
                    "required": str(_render_type(arg.get("type"))).endswith("!"),
                }
                for arg in node.get("args") or []
            ]
    return {"available": True, "field_count": len(fields), "fields": sorted(fields), "category_like": category_like}


def summarize_category_page(html: str) -> dict[str, Any]:
    """Structural summary of one aisle/landing page (no full HTML dumped)."""
    title = ""
    match = re.search(r"<title[^>]*>(?P<title>.*?)</title>", html, re.S | re.I)
    if match:
        title = " ".join(match.group("title").split())[:200]
    links = sorted({urllib.parse.urljoin(TESCO_SHOP_LANDING, m.group("url")) for m in _DRINK_LINK_RE.finditer(html)})
    blobs: list[dict[str, Any]] = []
    for match in _JSON_BLOB_RE.finditer(html):
        body = match.group("body").strip()
        entry: dict[str, Any] = {"chars": len(body)}
        try:
            parsed = json.loads(body)
        except ValueError:
            entry["shape"] = "unparsed"
        else:
            entry["shape"] = type(parsed).__name__
            if isinstance(parsed, dict):
                entry["keys"] = sorted(parsed.keys())[:40]
        blobs.append(entry)
    return {
        "title": title,
        "chars": len(html),
        "drink_links": links[:40],
        "json_blobs": blobs[:20],
        "category_values": sorted({m.group("value") for m in _CATEGORY_VALUE_RE.finditer(html)})[:40],
        "sample": html[:_HTML_SAMPLE_CHARS],
    }


def _run_batches(
    transport: Transport, api_key: str, operations: Sequence[Mapping[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Send operations in gateway-accepted chunks; return (records, results).

    The gateway rejects an oversized batch ("Batch size of 18 exceeds the
    maximum allowed size", run 35544158257), so every request stays inside
    ``TESCO_GRAPHQL_BATCH_LIMIT`` — the same constant the collection client
    hydrates with.
    """
    records: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    limit = TESCO_GRAPHQL_BATCH_LIMIT
    for start in range(0, len(operations), limit):
        chunk = list(operations[start:start + limit])
        record, payload = _attempt(
            transport, _graphql_batch_request(chunk, api_key), capture_errors=True
        )
        records.append(record)
        results.extend(_summarize_batch(chunk, payload))
    return records, results


def probe_graphql(
    transport: Transport,
    api_key: str,
    *,
    tpnb: str | None = None,
) -> dict[str, Any]:
    """Schema + operation reconnaissance against the storefront gateway.

    Three questions, cheapest first:

    1. Does the gateway answer a batch at all? The batch carries the
       production ``GetProductByTpnb`` operation as its **control**, so
       "gateway or key is broken" stays distinguishable from "this operation
       was rejected".
    2. Which category-ish field names exist, and which arguments do they
       demand? Answered by the gateway's own error text for argless queries.
    3. Is introspection available (a bonus: it short-circuits the above)?
    """
    report: dict[str, Any] = {}

    control = _first_tesco_tpnb(MAPPING_PATH) if tpnb is None else tpnb
    shape_operations, arg_operations = _scan_operations()
    operations: list[dict[str, Any]] = []
    if control:
        operations.append(
            {
                "operationName": "GetProductByTpnb",
                "variables": {"tpnb": control},
                "query": TESCO_PRODUCT_QUERY,
            }
        )
    operations.extend(shape_operations)

    batch_records, batch_results = _run_batches(transport, api_key, operations)
    report["control_tpnb"] = control
    report["batch"] = batch_records[0] if batch_records else {}
    report["batch_records"] = batch_records
    report["batch_results"] = batch_results
    by_operation = {str(row.get("operation")): row for row in batch_results}
    report["subfield_scan"] = [
        {
            "field": field,
            **classify_subfield_scan(by_operation.get(f"SubfieldScan_{field}")),
        }
        for field in CATEGORY_TARGET_FIELDS
    ]

    arg_records, arg_results = _run_batches(transport, api_key, arg_operations)
    report["arg_batch"] = arg_records[0] if arg_records else {}
    report["arg_batch_records"] = arg_records
    report["arg_scan_results"] = arg_results
    report["arg_scan"] = [
        classify_arg_scan(arg_results, field) for field in CATEGORY_TARGET_FIELDS
    ]

    intro_record, intro_payload = _attempt(
        transport,
        _graphql_batch_request(
            [
                {
                    "operationName": "CategoryWalkIntrospection",
                    "variables": {},
                    "query": INTROSPECTION_QUERY,
                }
            ],
            api_key,
        ),
        capture_errors=True,
    )
    report["introspection"] = intro_record
    report["introspection_summary"] = summarize_introspection(
        _sole_result(intro_payload)
    )
    return report


def probe_pages(transport: Transport, urls: Sequence[str]) -> list[dict[str, Any]]:
    """Fetch each candidate aisle/landing URL, recording a page summary."""
    pages: list[dict[str, Any]] = []
    for url in urls:
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-IE,en;q=0.9",
                "User-Agent": "drinks-tracker/0.1",
            },
        )
        record, body = _attempt(transport, request, parse_json=False)
        entry: dict[str, Any] = {"url": _clean_url(url), "attempt": record}
        if isinstance(body, (bytes, bytearray)):
            html = body.decode("utf-8", "replace")
            entry["page"] = summarize_category_page(html)
        pages.append(entry)
    return pages


def run(
    transport: Transport,
    *,
    api_key: str,
    out_dir: Path,
    category_urls: Sequence[str] = DEFAULT_CATEGORY_URLS,
) -> dict[str, Any]:
    """Run the whole probe and write the artifact directory."""
    pages = probe_pages(transport, category_urls)
    values: list[str] = []
    for page in pages:
        values.extend(page.get("page", {}).get("category_values") or [])
    graphql = probe_graphql(transport, api_key)
    summary: dict[str, Any] = {
        "probe": "tesco-category-walk",
        "endpoint": TESCO_GRAPHQL_ENDPOINT,
        "pages": pages,
        "page_category_values": values,
        "graphql": graphql,
        "note": (
            "Evidence only: no mappings, observations, or feed writes. "
            "Raw HTML is sampled, never dumped whole."
        ),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "probe-summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n"
    )
    introspection = graphql.get("introspection_summary") or {}
    (out_dir / "introspection-fields.json").write_text(
        json.dumps(introspection, indent=2, sort_keys=True) + "\n"
    )
    for index, page in enumerate(pages, start=1):
        (out_dir / f"category-page-{index}.json").write_text(
            json.dumps(page, indent=2, sort_keys=True) + "\n"
        )
    return summary


def build_transport(min_request_interval: float = 2.5) -> source_http.RetailerTransport:
    """Chrome-impersonated transport — the only one Tesco's edge accepts."""
    return source_http.RetailerTransport(
        "Tesco",
        opener=None,
        impersonate="chrome",
        min_request_interval=min_request_interval,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="beverage_feed probe",
        description=(
            "Tesco category-walk reconnaissance: capture the gateway schema and "
            "aisle page structure (manual, CI egress only, evidence artifact)"
        ),
    )
    parser.add_argument("--out", type=Path, default=Path("probe-artifact"))
    parser.add_argument(
        "--category-url",
        action="append",
        default=None,
        help="aisle URL to probe (repeatable; defaults to the Drinks candidates)",
    )
    parser.add_argument("--min-interval", type=float, default=2.5)
    parser.add_argument(
        "--api-key-env",
        default="TESCO_API_KEY",
        help="environment variable holding the Tesco GraphQL API key",
    )
    args = parser.parse_args(argv)

    api_key = os.environ.get(args.api_key_env, "").strip()
    urls = tuple(args.category_url) if args.category_url else DEFAULT_CATEGORY_URLS
    if not api_key:
        print(
            f"probe: {args.api_key_env} is not set — the GraphQL schema probe will "
            "record an auth error rather than pretending the route is healthy",
            file=sys.stderr,
        )
    summary = run(
        build_transport(min_request_interval=args.min_interval),
        api_key=api_key,
        out_dir=args.out,
        category_urls=urls,
    )
    graphql = summary["graphql"]
    results = graphql.get("batch_results") or []
    answered = [str(row["operation"]) for row in results if row.get("outcome") == "answered"]
    rejected = [str(row["operation"]) for row in results if row.get("outcome") == "rejected"]
    introspection = graphql.get("introspection_summary") or {}
    print(
        "probe: pages={pages} control_tpnb={tpnb} batch_answered={answered} "
        "batch_rejected={rejected} introspection={intro} -> {out}".format(
            pages=len(summary["pages"]),
            tpnb=graphql.get("control_tpnb"),
            answered=",".join(answered) or "none",
            rejected=",".join(rejected) or "none",
            intro="available" if introspection.get("available") else "unavailable",
            out=args.out,
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
