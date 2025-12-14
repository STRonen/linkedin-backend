from __future__ import annotations
import os
import logging
from dotenv import load_dotenv
import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol, Tuple
from urllib.parse import urlparse, urlunparse

import requests


# ----------------------------
# Types / Provider Interfaces
# ----------------------------

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


class GoogleSearchProvider(Protocol):
    def search(self, query: str, max_results: int = 10) -> List[SearchResult]:
        ...


class LinkedInProfileDataProvider(Protocol):
    def fetch_profile(self, profile_url: str) -> Dict[str, Any]:
        ...


# ---------------------------------
# Google Custom Search Provider
# ---------------------------------

class GoogleCustomSearchProvider:
    def __init__(self, api_key: str, cx: str, session: Optional[requests.Session] = None):
        self.api_key = api_key
        self.cx = cx
        self.session = session or requests.Session()

    def search(self, query: str, max_results: int = 10) -> List[SearchResult]:
        url = "https://www.googleapis.com/customsearch/v1"
        params = {
            "key": self.api_key,
            "cx": self.cx,
            "q": query,
            "num": min(max_results, 10),
        }
        resp = self.session.get(url, params=params, timeout=20)
        #print("REQUEST URL:", resp.url)
        resp.raise_for_status()

        data = resp.json()
        items = data.get("items", []) or []

        return [
            SearchResult(
                title=str(it.get("title", "") or ""),
                link=str(it.get("link", "") or ""),
                snippet=str(it.get("snippet", "") or ""),
            )
            for it in items
        ]


# ---------------------------------
# Query Builders (email optional)
# ---------------------------------

def _quoted_optional_terms(p: PersonInput) -> List[str]:
    return [
        f"\"{t.strip()}\""
        for t in (p.company_or_university, p.title_or_role, p.location)
        if t and t.strip()
    ]

def build_query_pass_a(p: PersonInput) -> str:
    """Primary search (no email)."""
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
    return re.sub(r"\s+", " ", " ".join(parts)).strip()

def build_query_pass_b(p: PersonInput) -> str:
    """Fallback search with email (only if email exists)."""
    if not p.email:
        return ""
    return f"{build_query_pass_a(p)} {p.email.strip()}"

def build_query_pass_c(p: PersonInput) -> str:
    """Last fallback allowing broader site scope."""
    parts = [
        "site:linkedin.com",
        "\"linkedin.com/in/\"",
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
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


# ---------------------------------
# Title must contain full name
# ---------------------------------

def _normalize_text(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()

def title_contains_full_name(title: str, full_name: str) -> bool:
    return _normalize_text(full_name) in _normalize_text(title)


# ---------------------------------
# URL normalization
# ---------------------------------

_PROFILE_RE = re.compile(r"^/(in|pub)/[^/]+/?$", re.IGNORECASE)

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


# ---------------------------------
# Candidate extraction
# ---------------------------------

def score_candidate(p: PersonInput, r: SearchResult) -> float:
    score = 0.6  # baseline since title already matches full name

    text = f"{r.title} {r.snippet}".lower()

    for field in (p.company_or_university, p.title_or_role, p.location):
        if field and field.lower() in text:
            score += 0.15

    if p.email and p.email.lower() in text:
        score += 0.35

    if "/in/" in (r.link or ""):
        score += 0.1

    return min(score, 1.0)

def extract_considered_profile_urls(
    p: PersonInput,
    results: List[SearchResult]
) -> List[str]:
    """
    Returns ONLY the list of LinkedIn profile URLs that were considered valid candidates.
    """

    urls: List[Tuple[float, str]] = []

    for r in results:
        # Enforce: title must contain full name
        if not title_contains_full_name(r.title, p.full_name):
            continue

        url = normalize_linkedin_profile_url(r.link)
        if not url:
            continue

        score = score_candidate(p, r)
        urls.append((score, url))

    # Sort by score (best first), remove duplicates while preserving order
    seen = set()
    ordered_urls: List[str] = []
    for _, u in sorted(urls, key=lambda x: x[0], reverse=True):
        if u not in seen:
            seen.add(u)
            ordered_urls.append(u)

    return ordered_urls



# ---------------------------------
# Main Orchestrator
# ---------------------------------

def linkedin_profile_finder(
    *,
    full_name: str,
    email: Optional[str] = None,
    location: Optional[str] = None,
    title_or_role: Optional[str] = None,
    company_or_university: Optional[str] = None,
    search_provider: GoogleSearchProvider,
    profile_provider: Optional[LinkedInProfileDataProvider] = None,
    max_results: int = 10,
) -> List[str]:

    if not full_name or not full_name.strip():
        raise ValueError("full_name is required")

    p = PersonInput(
        full_name=full_name,
        email=email,
        location=location,
        title_or_role=title_or_role,
        company_or_university=company_or_university,
    )

    queries = [
        build_query_pass_a(p),
        build_query_pass_b(p),
        build_query_pass_c(p),
    ]

    results: List[SearchResult] = []
    used_query = None

    for q in queries:
        if not q:
            continue
        results = search_provider.search(q, max_results=max_results)
        used_query = q
        if results:
            break

    urls_considered = extract_considered_profile_urls(p, results)

    return urls_considered



# ---------------------------------
# Example
# ---------------------------------

if __name__ == "__main__":
    load_dotenv("./Linkedin/linkedin-backend/linkedin-backend/.env")
    GOOGLE_API_KEY = os.getenv("Google_API_Key")
    GOOGLE_CX = os.getenv("CX")
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger("linkedin-search")
    search = GoogleCustomSearchProvider(GOOGLE_API_KEY, GOOGLE_CX)

    result = linkedin_profile_finder(
        full_name="Orly Sorokin",
        location="Israel",
        title_or_role="",
        company_or_university="IBM",
        search_provider=search,
    )

    print(json.dumps(result, indent=2))


