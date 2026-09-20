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
from typing import Any, Protocol, Sequence

from . import source_http
from .collector import safe_record

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

#: Argument names whose values we can supply without guessing a product
#: context. Anything absent from this map is reported as "no value known".
_ARG_VALUE_SOURCES = (
    "storeId", "store", "branchId", "branch", "servicePoint",
)


class Transport(Protocol):
    """The subset of ``RetailerTransport`` this probe uses."""

    def send(self, request: urllib.request.Request, *, parse_json: bool = True) -> Any:
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
) -> tuple[dict[str, Any], Any]:
    """Send one request, recording the outcome as artifact evidence."""
    record: dict[str, Any] = {
        "method": request.get_method(),
        "url": _clean_url(request.full_url),
    }
    try:
        payload = transport.send(request, parse_json=parse_json)
    except Exception as exc:  # evidence: the failure text is the finding
        record["outcome"] = "error"
        record["error"] = f"{type(exc).__name__}: {exc}"
        return record, None
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


def probe_graphql(
    transport: Transport,
    api_key: str,
    *,
    category_values: Sequence[str] = (),
) -> dict[str, Any]:
    """Introspect the gateway, then attempt a category call if it is safe to."""
    report: dict[str, Any] = {}
    record, payload = _attempt(transport, _graphql_request(INTROSPECTION_QUERY, api_key, None))
    report["introspection"] = record
    summary = summarize_introspection(payload)
    report["introspection_summary"] = summary
    if not summary.get("available"):
        report["category_call"] = {"outcome": "skipped", "reason": "introspection unavailable"}
        return report

    candidates = summary.get("category_like") or {}
    if not candidates:
        report["category_call"] = {
            "outcome": "skipped",
            "reason": "no category-like query field on the gateway",
        }
        return report

    field_name = sorted(candidates)[0]
    args = candidates[field_name]
    values: dict[str, str] = {}
    missing: list[str] = []
    for arg in args:
        name = str(arg.get("name"))
        if name in _ARG_VALUE_SOURCES:
            missing.append(f"{name}: {arg.get('type')} (no value known)")
            continue
        if arg.get("required"):
            if category_values:
                values[name] = category_values[0]
            else:
                missing.append(f"{name}: {arg.get('type')} (required, no category value found)")
            continue
        values[name] = category_values[0] if category_values else ""
    if missing:
        report["category_call"] = {
            "outcome": "not attempted",
            "field": field_name,
            "args": args,
            "missing": missing,
        }
        return report

    variables = {f"v{index}": value for index, value in enumerate(values.values())}
    arg_decls = ", ".join(f"{name}: $v{index}" for index, name in enumerate(values))
    var_decls = ", ".join(f"$v{index}: String" for index in range(len(values)))
    query = f"query CategoryWalkProbe({var_decls}) {{ {field_name}({arg_decls}) {{ __typename }} }}"
    record, _ = _attempt(transport, _graphql_request(query, api_key, variables))
    report["category_call"] = {"field": field_name, "args": args, "attempt": record}
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
    graphql = probe_graphql(transport, api_key, category_values=values)
    summary: dict[str, Any] = {
        "probe": "tesco-category-walk",
        "endpoint": TESCO_GRAPHQL_ENDPOINT,
        "pages": pages,
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
    introspection = summary["graphql"].get("introspection_summary") or {}
    category_like = introspection.get("category_like") or {}
    print(
        "probe: pages={pages} fields={fields} category_like={like} -> {out}".format(
            pages=len(summary["pages"]),
            fields=introspection.get("field_count", "unavailable"),
            like=",".join(sorted(category_like)) or "none",
            out=args.out,
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
