import os
import requests
from fastapi import FastAPI
from pydantic import BaseModel, HttpUrl
from dotenv import load_dotenv
from typing import Optional

load_dotenv()

SERPAPI_API_KEY = os.getenv("SERPAPI_API_KEY")

app = FastAPI(
    title="LinkedIn Profile Lookup Tool Backend (SerpAPI)",
    description="Uses SerpAPI to fetch real LinkedIn profile metadata",
    version="1.0.0",
)


class LinkedInLookupRequest(BaseModel):
    profile_url: HttpUrl


class LinkedInMetadata(BaseModel):
    full_name: Optional[str] = None
    headline: Optional[str] = None
    company: Optional[str] = None
    location: Optional[str] = None
    profile_url: Optional[str] = None


class LinkedInLookupResponse(BaseModel):
    status: str               # VALID | INVALID | INCONCLUSIVE
    metadata: Optional[LinkedInMetadata] = None
    error: Optional[str] = None


@app.post("/linkedin/lookup", response_model=LinkedInLookupResponse)
async def linkedin_profile_lookup(payload: LinkedInLookupRequest):
    """
    Lookup a LinkedIn profile using SerpAPI.

    Strategy:
    - If the URL is not a LinkedIn profile, return INVALID.
    - Use SerpAPI to query the LinkedIn profile.
    - If SerpAPI returns enough info, mark VALID and extract metadata.
    - If not found, mark INVALID.
    - If error/ambiguous, mark INCONCLUSIVE.
    """
    url = str(payload.profile_url)

    if "linkedin.com/in/" not in url and "linkedin.com/pub/" not in url:
        return LinkedInLookupResponse(
            status="INVALID",
            error="URL is not a LinkedIn profile.",
        )

    if not SERPAPI_API_KEY:
        return LinkedInLookupResponse(
            status="INCONCLUSIVE",
            error="SERPAPI_API_KEY is not configured on the server.",
        )

    # ---- Call SerpAPI ----
    try:
        serpapi_url = "https://serpapi.com/search"
        params = {
            "engine": "google",
            "q": url,
            "api_key": SERPAPI_API_KEY,
        }
        resp = requests.get(serpapi_url, params=params, timeout=15)

    except Exception as e:
        return LinkedInLookupResponse(
            status="INCONCLUSIVE",
            error=f"Error calling SerpAPI: {e}",
        )

    if resp.status_code != 200:
        return LinkedInLookupResponse(
            status="INCONCLUSIVE",
            error=f"SerpAPI HTTP {resp.status_code}: {resp.text}",
        )

    data = resp.json()

    # SerpAPI returns organic_results; we’ll look for the first result that
    # looks like a LinkedIn profile.
    organic = data.get("organic_results") or []
    linkedin_result = None
    for r in organic:
        link = r.get("link", "")
        if "linkedin.com/in/" in link or "linkedin.com/pub/" in link:
            linkedin_result = r
            break

    if not linkedin_result:
        # No LinkedIn result found – probably invalid or removed profile
        return LinkedInLookupResponse(
            status="INVALID",
            error="No LinkedIn result found via SerpAPI.",
        )

    title = linkedin_result.get("title", "")  # often "Name - Title - Company | LinkedIn"
    snippet = linkedin_result.get("snippet", "")
    displayed_link = linkedin_result.get("link", url)

    # Very rough parsing – you can improve heuristics if you want
    full_name = None
    headline = None
    company = None

    # Try to split title like: "Jane Doe - Senior Developer - IBM | LinkedIn"
    if " | LinkedIn" in title:
        stripped = title.replace(" | LinkedIn", "")
    else:
        stripped = title

    parts = [p.strip() for p in stripped.split(" - ") if p.strip()]
    if parts:
        full_name = parts[0]
    if len(parts) >= 2:
        headline = parts[1]
    if len(parts) >= 3:
        company = parts[2]

    metadata = LinkedInMetadata(
        full_name=full_name,
        headline=headline or snippet,
        company=company,
        location=None,          # SerpAPI doesn’t directly give location here
        profile_url=displayed_link,
    )

    return LinkedInLookupResponse(
        status="VALID",
        metadata=metadata,
        error=None,
    )
