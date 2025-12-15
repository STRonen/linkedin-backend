import os
import re
from dataclasses import dataclass
from typing import List, Optional
from urllib.parse import urlparse, urlunparse

import requests


# =========================================================
# Data models
# =========================================================

@dataclass(frozen=True)
class PersonInput:
    full_name: str
    email: Optional[str] = None
    location: Optional[str] = None
    title_or_role: Optional[str] = None
    company_or_university: Optional[str] = None


@dataclass(frozen=True)
class SearchResult:
    title: str
    link: str
    snippet: str = ""


# =========================================================
# Google Custom Search
# =========================================================

class GoogleCustomSearchProvider:
    def __init__(self):
        self.api_key = os.environ.get("GOOGLE_API_KEY")
        self.cx = os.environ.get("GOOGLE_CX")

        if not self.api_key or not self.cx:
            raise RuntimeError("GOOGLE_API_KEY and GOOGLE_CX must be set")

        self.session = requests.Session()

    def search(self, query: str, max_results: int = 10) -> List[SearchResult]:
        url = "https://www.googleapis.com/customsearch/v1"
        params = {
            "key": self.api_key,
            "cx": self.cx,
            "q": query,
            "num": min(max_results, 10),
        }

        resp = self.session.get(url, params=params, timeout=20)
        resp.raise_for_status()

        data = resp.json()
        items = data.get("items", []) or []

        return [
            SearchResult(
                title=str(it.get("title", "")),
                link=str(it.get("link", "")),
                snippet=str(it.get("snippet", "")),
            )
            for it in items
        ]


# =========================================================
# Query builders
# =========================================================

def _quoted_optional_terms(p: PersonInput) -> List[str]:
    return [
        f"\"{t.strip()}\""
        for t in (p.company_or_university, p.title_or_role, p.location)
        if t and t.strip()
    ]

def build_query(p: PersonInput) -> str:
    parts = [
        "site:linkedin.com/in",
        f"\"{p.full_name.strip()}\"",
        *_quoted_optional_terms(p),
        "-inurl:/company/",
        "-inurl:/posts/",
        "-inurl:/jobs/",
        "-inurl:/pulse/",
        "-inurl:/learning/",
        "-inurl:/groups/",
        "-inurl:/directory/",
        "-inurl:/school/",
    ]

    if p.email:
        parts.append(p.email.strip())

    return re.sub(r"\s+", " ", " ".join(parts)).strip()


# =========================================================
# Filtering & normalization
# =========================================================

_PROFILE_RE = re.compile(r"^/(in|pub)/[^/]+/?$", re.IGNORECASE)

def _normalize_text(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()

def title_contains_full_name(title: str, full_name: str) -> bool:
    return _normalize_text(full_name) in _normalize_text(title)

def normalize_linkedin_profile_url(url: str) -> Optional[str]:
    try:
        u = urlparse(url)
    except Exception:
        return None

    if "linkedin.com" not in (u.netloc or ""):
        return None

    if not _PROFILE_RE.match(u.path or ""):
        return None

    path = re.sub(r"/+$", "", u.path) + "/"
    return urlunparse(("https", "www.linkedin.com", path, "", "", ""))


# =========================================================
# Core logic
# =========================================================

def find_linkedin_profile_urls(p: PersonInput, search_provider) -> List[str]:
    query = build_query(p)
    results = search_provider.search(query)

    scored_urls = []

    for r in results:
        if not title_contains_full_name(r.title, p.full_name):
            continue

        url = normalize_linkedin_profile_url(r.link)
        if not url:
            continue

        scored_urls.append(url)

    # Deduplicate while preserving order
    seen = set()
    ordered = []
    for u in scored_urls:
        if u not in seen:
            seen.add(u)
            ordered.append(u)

    return ordered


# =========================================================
# Code Engine Function entrypoint
# =========================================================

def main(params):
    """
    IBM Code Engine Functions entrypoint.

    Expected JSON input:
    {
      "full_name": "Ronen Siman Tov",
      "email": "optional@email.com",
      "location": "Tel Aviv",
      "title_or_role": "CTO",
      "company_or_university": "IBM"
    }

    Output:
    [
      "https://www.linkedin.com/in/ronen-siman-tov/"
    ]
    """

    full_name = params.get("full_name")
    if not full_name or not isinstance(full_name, str):
        return {
            "statusCode": 400,
            "body": "full_name is required"
        }

    p = PersonInput(
        full_name=full_name,
        email=params.get("email"),
        location=params.get("location"),
        title_or_role=params.get("title_or_role"),
        company_or_university=params.get("company_or_university"),
    )

    try:
        search_provider = GoogleCustomSearchProvider()
        urls = find_linkedin_profile_urls(p, search_provider)

        return {
            "linkedin_profile_url": urls[0] if urls else None,
            "all_urls": urls
        }


    except Exception as e:
        return {
            "statusCode": 500,
            "body": str(e)
        }

