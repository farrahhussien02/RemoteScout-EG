#!/usr/bin/env python3
"""Build a fresh, Egypt-eligible remote jobs snapshot from public, attributed feeds."""
import concurrent.futures
import datetime as dt
import html
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from urllib.parse import urlparse
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "jobs.json")
NOW = dt.datetime.now(dt.timezone.utc)
HEADERS = {"User-Agent": "RemoteScoutEG/1.0 (+https://farrahhussien02.github.io/RemoteScout-EG/)"}
EXCLUSIONS = re.compile(r"\b(not? (?:available|open|eligible|hiring|accepting) (?:in|from) egypt|excluding egypt|except egypt|no egypt|us only|usa only|united states only|canada only|uk only|europe only|eu only|must be based in (?:the )?(?:us|usa|united states|canada|uk|europe))\b", re.I)
WORLD = re.compile(r"\b(worldwide|anywhere|global|work from anywhere|all countries)\b", re.I)
AFRICA = re.compile(r"\b(africa|african countries)\b", re.I)
EGYPT = re.compile(r"\b(egypt|egyptian)\b", re.I)

def get_json(url, params=None):
    response = requests.get(url, params=params, headers=HEADERS, timeout=35)
    response.raise_for_status()
    return response.json()

def clean_text(value):
    return html.unescape(re.sub(r"<[^>]+>", " ", str(value or ""))).replace("\xa0", " ")

def allowed(location, description=""):
    loc = clean_text(location)
    body = clean_text(description)
    combined = loc + " " + body
    if EXCLUSIONS.search(combined):
        return False, "Excluded Egypt"
    if EGYPT.search(loc):
        return True, "Egypt listed"
    if AFRICA.search(loc):
        return True, "Africa listed"
    if WORLD.search(loc):
        return True, "Worldwide listed"
    return False, "Egypt eligibility not explicit"

def iso_date(value):
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(value, dt.timezone.utc)
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(dt.timezone.utc)
    except Exception:
        return None

def record(source, title, company, url, location, description, published, category="", job_type="", salary="", source_url=None, id_value=None, expiry=None):
    title = clean_text(title).strip()
    url = str(url or "").strip()
    if not title or not url.startswith("https://"):
        return None
    ok, eligibility = allowed(location, description)
    if not ok:
        return None
    pub = iso_date(published)
    if pub and pub < NOW - dt.timedelta(days=45):
        return None
    exp = iso_date(expiry)
    if exp and exp < NOW:
        return None
    return {
        "id": str(id_value or url), "title": title, "company_name": clean_text(company).strip() or "Company not specified",
        "url": url, "candidate_required_location": clean_text(location).strip() or "Worldwide",
        "description": str(description or ""), "publication_date": pub.isoformat() if pub else "",
        "category": clean_text(category).strip() or "Other", "job_type": clean_text(job_type).strip(),
        "salary": clean_text(salary).strip(), "source": source, "source_url": source_url or url,
        "eligibility": eligibility, "expiry_date": exp.isoformat() if exp else ""
    }

def fetch_himalayas():
    out, page = [], 1
    while page <= 20:
        data = get_json("https://himalayas.app/jobs/api/search", {"country": "EG", "page": page})
        jobs = data.get("jobs", [])
        if not jobs:
            break
        for j in jobs:
            restrictions = j.get("locationRestrictions") or []
                        # Himalayas country search includes Egypt-restricted jobs plus unrestricted worldwide jobs.
            # Egypt standard time is UTC+2; skip listings with incompatible explicit timezone limits.
            tz = j.get("timezoneRestrictions") or []
            if tz and not any(str(x).strip().upper().replace("UTC", "").replace("+", "").replace(" ", "") in ("2", "2.0") for x in tz):
                continue
            location = "Egypt" if any(str(x).lower() in ("egypt", "eg") for x in restrictions) else ("Worldwide" if not restrictions else ", ".join(restrictions))
            desc = j.get("description") or j.get("excerpt") or ""
            salary = ""
            if j.get("minSalary") is not None or j.get("maxSalary") is not None:
                salary = "%s %s-%s %s" % (j.get("currency") or "", j.get("minSalary") or "", j.get("maxSalary") or "", j.get("salaryPeriod") or "annual")
            item = record("Himalayas", j.get("title"), j.get("companyName"), j.get("applicationLink"), location, desc, j.get("pubDate"), ", ".join(j.get("categories") or j.get("parentCategories") or []), j.get("employmentType"), salary, "https://himalayas.app/", j.get("guid"), j.get("expiryDate"))
            if item: out.append(item)
        if len(jobs) < 20:
            break
        page += 1
        time.sleep(0.15)
    return out

def fetch_jobicy():
    data = get_json("https://jobicy.com/api/jobs", {"count": 200, "geo": "anywhere"})
    out = []
    for j in data.get("jobs", []):
        loc = j.get("jobGeo") or ""
        desc = j.get("jobDescription") or j.get("jobExcerpt") or ""
        salary = ""
        if j.get("salaryMin") or j.get("salaryMax"):
            salary = "%s %s-%s %s" % (j.get("salaryCurrency", ""), j.get("salaryMin", ""), j.get("salaryMax", ""), j.get("salaryPeriod", ""))
        item = record("Jobicy", j.get("jobTitle"), j.get("companyName"), j.get("url"), loc, desc, j.get("pubDate"), ", ".join(j.get("jobIndustry") or []), ", ".join(j.get("jobType") or []), salary, "https://jobicy.com/jobs-rss-feed", j.get("id"))
        if item: out.append(item)
    return out

class RSSText(HTMLParser):
    def __init__(self):
        super().__init__(); self.parts = []
    def handle_data(self, data):
        self.parts.append(data)

def fetch_wwr():
    url = "https://weworkremotely.com/remote-jobs.rss"
    response = requests.get(url, headers=HEADERS, timeout=35)
    response.raise_for_status()
    root = ET.fromstring(response.content)
    out = []
    for node in root.findall(".//item"):
        get = lambda name: (node.findtext(name) or "").strip()
        description = get("description")
        parser = RSSText(); parser.feed(description)
        body = " ".join(parser.parts)
        title = get("title")
        # WWR's feed must explicitly state an eligible geography; generic 'remote' is not enough.
        match = re.search(r"\b(worldwide|anywhere|africa|egypt)\b", title + " " + body, re.I)
        location = match.group(1) if match else ""
        item = record("We Work Remotely", title, "", get("link"), location, body, get("pubDate"), "Remote", "", "", "https://weworkremotely.com/remote-job-rss-feed", get("guid"))
        if item: out.append(item)
    return out


# Public AI-training listings feed, reusable under CC BY 4.0 with attribution.
AI_PROJECT_PLATFORM_MATCH = {
    "meridial": "Meridial",
    "alignerr": "Alignerr",
    "oneforma": "OneForma",
    "outlier": "Outlier",
    "rws": "RWS TrainAI",
    "dataforce": "DataForce",
    "appen": "CrowdGen by Appen",
    "crowdgen": "CrowdGen by Appen",
    "mindrift": "Mindrift",
    "telus": "TELUS Digital AI Community",
}

def first_value(obj, *keys):
    for key in keys:
        value = obj.get(key)
        if value not in (None, "", [], {}):
            return value
    return None

def country_is_eligible(value):
    text_value = ", ".join(str(x) for x in value) if isinstance(value, list) else str(value or "")
    text_value = clean_text(text_value)
    if not text_value.strip():
        return False, "Country eligibility not disclosed"
    if EXCLUSIONS.search(text_value):
        return False, "Egypt excluded"
    if EGYPT.search(text_value):
        return True, "Egypt listed"
    if re.search(r"\b(worldwide|global|all countries|any country)\b", text_value, re.I):
        return True, "Worldwide listed"
    if AFRICA.search(text_value):
        return True, "Africa listed"
    return False, "Egypt not listed"

def fetch_ai_training_projects():
    out, offset, total = [], 0, None
    while total is None or offset < total:
        data = get_json("https://aitrainer.work/api/jobs.json",
                        {"limit": 500, "offset": offset, "compact": 1})
        if isinstance(data, list):
            rows = data
            total = len(rows)
        else:
            rows = data.get("jobs") or data.get("results") or data.get("data") or []
            total = int(data.get("total", len(rows)))
        if not rows:
            break
        for j in rows:
            if not isinstance(j, dict):
                continue
            platform_raw = str(first_value(j, "platform", "platform_name", "source") or "").lower()
            platform_name = next((name for key, name in AI_PROJECT_PLATFORM_MATCH.items() if key in platform_raw.replace(" ", "").replace("-", "")), None)
            if not platform_name:
                continue
            countries = first_value(j, "eligible_countries", "eligibleCountries", "countries", "locations", "location")
            ok, eligibility = country_is_eligible(countries)
            if not ok:
                continue
            first_seen = iso_date(first_value(j, "first_seen", "firstSeen", "first_seen_at"))
            last_seen = iso_date(first_value(j, "last_seen", "lastSeen", "last_seen_at"))
            # "First seen" is the feed's observation date, not necessarily the employer's
            # publication date. Require recent observation and a recent open-status check.
            if not first_seen or first_seen < NOW - dt.timedelta(days=7):
                continue
            if not last_seen or last_seen < NOW - dt.timedelta(days=2):
                continue
            published = iso_date(first_value(j, "published_at", "publication_date", "posted_at", "date_posted"))
            if published and published < NOW - dt.timedelta(days=30):
                continue
            title = first_value(j, "title", "job_title", "name")
            company = first_value(j, "company", "company_name", "employer") or platform_name
            url = first_value(j, "url", "job_url", "link")
            if not title or not url or "aitrainer.work" not in urlparse(str(url)).netloc.lower():
                continue
            country_text = ", ".join(countries) if isinstance(countries, list) else str(countries or "")
            applicants = first_value(j, "applicant_count", "applicants", "applications_count")
            out.append({
                "id": str(first_value(j, "id", "job_id") or url),
                "title": clean_text(title).strip(),
                "company_name": clean_text(company).strip(),
                "url": str(url),
                "candidate_required_location": clean_text(country_text),
                "description": str(first_value(j, "description", "summary", "excerpt") or ""),
                "publication_date": published.isoformat() if published else "",
                "first_seen": first_seen.isoformat(),
                "last_seen": last_seen.isoformat(),
                "category": clean_text(first_value(j, "domain", "field", "category") or "AI training"),
                "job_type": clean_text(first_value(j, "contract_type", "job_type", "type") or "contract"),
                "salary": clean_text(first_value(j, "pay_text", "salary", "rate") or ""),
                "source": platform_name,
                "source_url": "https://aitrainer.work/open-data",
                "eligibility": eligibility,
                "applicant_count": applicants if isinstance(applicants, (int, float)) else None,
                "competition_note": "Applicant count not disclosed" if applicants is None else "Applicant count supplied by source",
                "link_status": "source_feed"
            })
        offset = data.get("next_offset") if isinstance(data, dict) and data.get("next_offset") is not None else offset + len(rows)
        if not rows or offset <= 0:
            break
        if total is not None and offset >= total:
            break
        time.sleep(1.5)
    deduped = {}
    for item in out:
        key = item["url"].split("?")[0].rstrip("/").lower()
        deduped[key] = item
    return list(deduped.values())

def check_link(job):
    try:
        r = requests.get(job["url"], headers=HEADERS, timeout=12, allow_redirects=True, stream=True)
        status = r.status_code
        r.close()
        if status in (404, 410) or status >= 500:
            return None
        if 200 <= status < 400:
            job["link_status"] = "checked"
        elif status in (403, 429):
            job["link_status"] = "reachable_check_limited"
        else:
            return None
        return job
    except requests.RequestException:
        return None

def main():
    sources = [
        {"name":"Himalayas","url":"https://himalayas.app/","api":"https://himalayas.app/docs/remote-jobs-api","note":"Country-filtered for Egypt; worldwide jobs are included by the source API."},
        {"name":"Jobicy","url":"https://jobicy.com/","api":"https://jobicy.com/jobs-rss-feed","note":"Public worldwide remote-jobs API; worldwide eligibility is checked against listing text."},
        {"name":"We Work Remotely","url":"https://weworkremotely.com/","api":"https://weworkremotely.com/remote-job-rss-feed","note":"Official public RSS; only explicit Egypt, Africa, or worldwide locations are included."}
    ]
    collected, errors = [], []
    project_jobs = []
    for name, fn in [("Himalayas", fetch_himalayas), ("Jobicy", fetch_jobicy), ("We Work Remotely", fetch_wwr)]:
        try:
            batch = fn()
            collected.extend(batch)
            print("%s: %s eligible listings" % (name, len(batch)))
        except Exception as exc:
            errors.append({"source":name,"error":str(exc)[:240]})
            print("%s failed: %s" % (name, exc))
    try:
        project_jobs = fetch_ai_training_projects()
        print("AI training project feed: %s fresh Egypt-eligible listings" % len(project_jobs))
    except Exception as exc:
        errors.append({"source":"AI training project feed","error":str(exc)[:240]})
        print("AI training project feed failed: %s" % exc)
    deduped = {}
    for job in collected:
        key = job["url"].split("?")[0].rstrip("/").lower()
        if key not in deduped or (job.get("publication_date","") > deduped[key].get("publication_date","")):
            deduped[key] = job
    candidates = list(deduped.values())
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        checked = [x for x in pool.map(check_link, candidates) if x]
    checked.sort(key=lambda j: j.get("publication_date",""), reverse=True)
    payload = {
        "updated_at": NOW.isoformat(), "timezone": "UTC",
        "job_count": len(checked), "sources": sources,
        "jobs": checked,
        "project_job_count": len(project_jobs), "project_jobs": project_jobs,
        "project_feed_attribution": "AI training listings from aitrainer.work/open-data, licensed under CC BY 4.0.",
        "update_note": "Automatically refreshed daily. Listings are re-read from their sources; expired, missing, broken, and explicitly Egypt-excluding listings are removed.",
        "source_errors": errors
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    print("Saved %s verified listings to %s" % (len(checked), OUT))

if __name__ == "__main__":
    main()
