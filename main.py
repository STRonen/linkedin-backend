import os
import logging
from typing import Optional, Literal, List, Any, Dict

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, HttpUrl

# -------------------------------------------------------------------
# Setup
# -------------------------------------------------------------------

load_dotenv()

SERPAPI_API_KEY = os.getenv("SERPAPI_API_KEY")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("linkedin-backend")

app = FastAPI(
    title="LinkedIn Profile Lookup Tool Backend (SerpAPI)",
    description="Uses SerpAPI to fetch real LinkedIn profile metadata",
    version="2.0.0",
)


# -------------------------------------------------------------------
# Pydantic models
# -------------------------------------------------------------------

class LinkedInMetadata(BaseModel):
    full_name: Optional[str]
    headline: Optional[str]
    company: Optional[str]
    location: Optional[str]
    profile_url: Optional[HttpUrl]


class LinkedInLookupRequest(BaseModel):
    profile_url: HttpUrl


class LinkedInLookupResponse(BaseModel):
    status: Literal["VALID", "INVALID", "ERROR"]
    metadata: Optional[LinkedInMetadata]
    error: Optional[str]


# -------------------------------------------------------------------
# Helper functions
# -------------------------------------------------------------------

def is_valid_linkedin_profile_url(url: str) -> bool:
    """Basic validation: linkedin.com/in/... or linkedin.com/pub/..."""
    url_lower = url.lower()
    if "linkedin.com" not in url_lower:
        return False

    # Very simple path check
    # Accept both /in/ and /pub/ styles
    return "/in/" in url_lower or "/pub/" in url_lower


def extract_full_name(title: str) -> str:
    """
    Try to grab a human name from the search result title.
    Common patterns:
    - "Name - Job Title - Company | LinkedIn"
    - "Name | LinkedIn"
    """
    # Remove trailing LinkedIn branding
    title = title.replace(" | LinkedIn", "").replace("| LinkedIn", "").strip()

    # Often "Name - Job title - Company"
    if " - " in title:
        first_part = title.split(" - ", 1)[0]
        return first_part.strip()

    # Fallback: whole title
    return title.strip()


def extract_from_extensions(extensions: List[str]) -> (Optional[str], Optional[str]):
    """
    Heuristic parsing of SerpAPI rich_snippet 'extensions', which often look like:
    ["Job Title", "Company Name", "Location, Country"]
    """
    if not extensions:
        return None, None

    company = None
    location = None

    if len(extensions) >= 3:
        # Last is usually location (contains comma or country)
        location = extensions[-1].strip()
        # Middle often company
        company = extensions[-2].strip()
    elif len(extensions) == 2:
        # [Job Title, Company] or [Company, Location]
        maybe_company = extensions[1].strip()
        if "," in maybe_company or " • " in maybe_company or " · " in maybe_company:
            location = maybe_company
        else:
            company = maybe_company

    return company, location


def extract_company_and_location(
    result: Dict[str, Any]
) -> (Optional[str], Optional[str]):
    """
    Try multiple sources:
    1. rich_snippet.top.extensions (best structured)
    2. title patterns
    3. snippet with 'at'
    """
    title: str = result.get("title", "") or ""
    snippet: str = result.get("snippet", "") or ""

    company: Optional[str] = None
    location: Optional[str] = None

    # 1. Try rich_snippet extensions
    rich_snippet = result.get("rich_snippet") or {}
    top = rich_snippet.get("top") or {}
    extensions = (
        top.get("extensions")
        or rich_snippet.get("extensions")
        or []
    )

    if isinstance(extensions, list):
        ext_company, ext_location = extract_from_extensions(extensions)
        if ext_company:
            company = ext_company
        if ext_location:
            location = ext_location

    # 2. If still no company, try title pattern:
    #    "Name - Role - Company | LinkedIn"
    if not company and " - " in title:
        cleaned_title = title.replace(" | LinkedIn", "").replace("| LinkedIn", "")
        parts = [p.strip() for p in cleaned_title.split(" - ") if p.strip()]
        if len(parts) >= 3:
            company_candidate = parts[-1]
            if company_candidate and not company_candidate.lower().startswith("linkedin"):
                company = company_candidate

    # 3. If still no company, try snippet "at <Company>"
    if not company and " at " in snippet:
        # E.g.: "Head of Something at Big Company. Based in ..."
        after_at = snippet.split(" at ", 1)[1]
        # Cut at punctuation / separators
        for sep in [".", "|", " - ", " • ", " · "]:
            if sep in after_at:
                after_at = after_at.split(sep, 1)[0]
        company_candidate = after_at.strip()
        if company_candidate:
            company = company_candidate

    # Location from snippet: look for something that "looks like" a place
    # (very heuristic)
    if not location:
        # Sometimes location appears after "based in"
        lowered_snippet = snippet.lower()
        if "based in " in lowered_snippet:
            after = snippet.split("based in ", 1)[1]
            for sep in [".", "|", " - ", " • ", " · "]:
                if sep in after:
                    after = after.split(sep, 1)[0]
            loc_candidate = after.strip()
            if loc_candidate:
                location = loc_candidate

    return company, location


def pick_best_linkedin_result(organic_results: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    From SerpAPI 'organic_results', pick the first result clearly pointing
    to a LinkedIn public profile (linkedin.com/in/...).
    """
    if not organic_results:
        return None

    linkedin_results = []
    for r in organic_results:
        link = r.get("link") or ""
        displayed = r.get("displayed_link") or ""
        if "linkedin.com/in/" in link.lower() or "linkedin.com/in/" in displayed.lower():
            linkedin_results.append(r)

    if linkedin_results:
        return linkedin_results[0]

    return None


# -------------------------------------------------------------------
# Routes
# -------------------------------------------------------------------

@app.get("/health", description="Health check endpoint for monitoring")
def health():
    return {"status": "ok"}


@app.post("/lookup", response_model=LinkedInLookupResponse, escription="Lookup a LinkedIn profile and return metadata including name, headline, company, and location.")
def lookup_linkedin_profile(request: LinkedInLookupRequest) -> LinkedInLookupResponse:
    if not SERPAPI_API_KEY:
        logger.error("SERPAPI_API_KEY is not configured on the server")
        raise HTTPException(
            status_code=500,
            detail="SERPAPI_API_KEY is not configured on the server",
        )

    profile_url = str(request.profile_url)
    if not is_valid_linkedin_profile_url(profile_url):
        return LinkedInLookupResponse(
            status="INVALID",
            metadata=None,
            error="profile_url is not a valid LinkedIn profile URL",
        )

    logger.info(f"Looking up LinkedIn profile via SerpAPI: {profile_url}")

    params = {
        "engine": "google",
        "q": profile_url,
        "num": 5,
        "api_key": SERPAPI_API_KEY,
    }

    try:
        serp_resp = requests.get("https://serpapi.com/search", params=params, timeout=15)
    except requests.RequestException as e:
        logger.exception("Error calling SerpAPI")
        return LinkedInLookupResponse(
            status="ERROR",
            metadata=None,
            error=f"Error calling SerpAPI: {e}",
        )

    if serp_resp.status_code != 200:
        logger.error(f"SerpAPI returned non-200: {serp_resp.status_code}")
        return LinkedInLookupResponse(
            status="ERROR",
            metadata=None,
            error=f"SerpAPI returned HTTP {serp_resp.status_code}",
        )

    data = serp_resp.json()
    organic_results = data.get("organic_results") or []

    best = pick_best_linkedin_result(organic_results)
    if not best:
        logger.info("No LinkedIn result found in SerpAPI response")
        return LinkedInLookupResponse(
            status="INVALID",
            metadata=None,
            error="No LinkedIn profile result found for this URL",
        )

    title: str = best.get("title", "") or ""
    snippet: str = best.get("snippet", "") or ""
    link: str = best.get("link") or best.get("displayed_link") or profile_url

    full_name = extract_full_name(title)
    headline = snippet or title

    company, location = extract_company_and_location(best)

    metadata = LinkedInMetadata(
        full_name=full_name or None,
        headline=headline or None,
        company=company or None,
        location=location or None,
        profile_url=link,
    )

    return LinkedInLookupResponse(
        status="VALID",
        metadata=metadata,
        error=None,
    )


# Optional: useful for local debugging/run
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8080")),
        reload=True,
    )
