"""Optional website directory for companies listed without a website: Wikidata.

Wikidata is a free, openly licensed (CC0) knowledge base; its "official website" property
(P856) is the directory used here. One request per company goes to the public query service
(``query.wikidata.org``) through the same protected fetcher as every other page (robots.txt,
HTTPS, public-address and TLS checks all apply). No API key, no paid service.

A search result is only a lead. An entry becomes a candidate only when its English label or one
of its English aliases equals the company name exactly after Unicode normalization, case
folding and whitespace collapsing, so "Acme Labs" never stands in for "Acme" and an acronym is
accepted only when the entry itself lists it as an alias. Several matching entries on different
domains are left for review by discovery; a candidate website is then verified on the website
itself (``collector.companies.discovery``). Nothing is invented when there is no match.
"""

import json
import re
from collections.abc import Generator
from typing import Any
from urllib.parse import urlencode

from collector.companies.discovery import Candidate, Directory, FetchError, Page
from collector.companies.input import Company, check_https_url, company_key

ENDPOINT = "https://query.wikidata.org/sparql"
PROVIDERS = ("none", "wikidata")
MAX_BINDINGS = 1000

# Entity search (the same search as the Wikidata site box) for at most 20 items, then each
# item's official websites and English label and aliases. Exact matching happens in Python.
_QUERY = """SELECT ?item ?label ?alias ?website WHERE {
  SERVICE wikibase:mwapi {
    bd:serviceParam wikibase:endpoint "www.wikidata.org";
                    wikibase:api "EntitySearch";
                    mwapi:search NAME;
                    mwapi:language "en";
                    mwapi:limit "20".
    ?item wikibase:apiOutputItem mwapi:item.
  }
  ?item wdt:P856 ?website.
  OPTIONAL { ?item rdfs:label ?label. FILTER(LANG(?label) = "en") }
  OPTIONAL { ?item skos:altLabel ?alias. FILTER(LANG(?alias) = "en") }
}"""
_ITEM = re.compile(r"http://www\.wikidata\.org/entity/(Q[1-9][0-9]*)")


class WikidataDirectory:
    name = "wikidata"

    def query_url(self, company_name: str) -> str:
        # The name becomes one SPARQL string literal: json.dumps escapes quotes and backslashes,
        # and company names never contain control characters (input validation).
        literal = json.dumps(company_name, ensure_ascii=False)
        query = _QUERY.replace("NAME", literal, 1)
        return f"{ENDPOINT}?{urlencode({'format': 'json', 'query': query})}"

    def lookup(self, company: Company) -> Generator[str, Page, tuple[Candidate, ...]]:
        page = yield self.query_url(company.name)
        return parse_candidates(page.html, company.name)


def parse_candidates(text: str, company_name: str) -> tuple[Candidate, ...]:
    """Candidates from a SPARQL JSON result: entries named exactly ``company_name`` with a
    public HTTPS official website. Raises ``FetchError`` for an unusable response."""
    try:
        data: Any = json.loads(text)
        bindings = data["results"]["bindings"]
    except (ValueError, KeyError, TypeError):
        raise FetchError("directory response is not a SPARQL JSON result") from None
    if not isinstance(bindings, list):
        raise FetchError("directory response is not a SPARQL JSON result")
    items: dict[str, dict[str, set[str]]] = {}
    for binding in bindings[:MAX_BINDINGS]:
        if not isinstance(binding, dict):
            continue
        match = _ITEM.fullmatch(_value(binding, "item") or "")
        if match is None:
            continue
        item = items.setdefault(match.group(1), {"label": set(), "alias": set(), "website": set()})
        for field in ("label", "alias", "website"):
            value = _value(binding, field)
            if value:
                item[field].add(value)
    wanted = company_key(company_name)
    found: list[Candidate] = []
    for qid in sorted(items, key=lambda q: int(q[1:])):
        item = items[qid]
        matching = [name for name in item["label"] | item["alias"] if company_key(name) == wanted]
        if not matching:
            continue  # a search hit, not this company
        names = tuple(sorted(item["label"])) + tuple(sorted(set(matching) - item["label"]))
        for website in sorted(item["website"]):
            try:
                url = check_https_url(website, "website")
            except ValueError:
                continue  # http-only or unsafe: never upgraded or guessed
            if url is not None:
                found.append(Candidate(url, names, f"Wikidata {qid}"))
    return tuple(found)


def _value(binding: dict[str, Any], field: str) -> str | None:
    entry = binding.get(field)
    if isinstance(entry, dict) and isinstance(entry.get("value"), str):
        return entry["value"]
    return None


def get_directory(name: str) -> Directory | None:
    """The configured directory (``"none"``: no lookup). Raises ``ValueError`` if unknown."""
    if name == "none":
        return None
    if name == "wikidata":
        return WikidataDirectory()
    raise ValueError(f"unknown website directory; choose one of {', '.join(PROVIDERS)}")
