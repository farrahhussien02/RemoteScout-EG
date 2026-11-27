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


def fetch_remoteok():
    data = get_json("https://remoteok.com/api")
    out = []
    for j in data if isinstance(data, list) else []:
        if not isinstance(j, dict) or not j.get("position"):
            continue
        location = j.get("location") or ""
        description = j.get("description") or ""
        item = record(
            "Remote OK", j.get("position"), j.get("company"), j.get("apply_url") or j.get("url"),
            location, description, j.get("date"), ", ".join(j.get("tags") or []), "",
            "%s-%s" % (j.get("salary_min") or "", j.get("salary_max") or "") if j.get("salary_min") or j.get("salary_max") else "",
            "https://remoteok.com/faq", j.get("id") or j.get("slug")
        )
        if item:
            out.append(item)
    return out


def fetch_remotive():
    data = get_json("https://remotive.com/api/remote-jobs")
    out = []
    for j in data.get("jobs", []) if isinstance(data, dict) else []:
        item = record(
            "Remotive", j.get("title"), j.get("company_name"), j.get("url"),
            j.get("candidate_required_location") or "", j.get("description") or "",
            j.get("publication_date"), j.get("category") or "", j.get("job_type") or "",
            j.get("salary") or "", "https://remotive.com/remote-jobs/api", j.get("id")
        )
        if item:
            out.append(item)
    return out


def fetch_working_nomads():
    rows = get_json("https://www.workingnomads.com/api/exposed_jobs/")
    out = []
    for j in rows if isinstance(rows, list) else []:
        item = record(
            "Working Nomads", j.get("title"), j.get("company_name"), j.get("url"),
            j.get("location") or "", j.get("description") or "", j.get("pub_date"),
            j.get("category_name") or "", "", "", "https://www.workingnomads.com/jobs", j.get("id")
        )
        if item:
            out.append(item)
    return out


def fetch_wing_assistant():
    rows = get_json("https://api.lever.co/v0/postings/getwingapp", {"mode": "json"})
    out = []
    for j in rows if isinstance(rows, list) else []:
        categories = j.get("categories") or {}
        location = str(categories.get("location") or "")
        description = j.get("descriptionPlain") or j.get("description") or ""
        created = j.get("createdAt")
        published = iso_date(created / 1000 if isinstance(created, (int, float)) and created > 100000000000 else created)
        item = record(
            "Wing Assistant", j.get("text") or j.get("title"), "Wing Assistant",
            j.get("hostedUrl") or j.get("applyUrl"), location, description, published,
            categories.get("team") or "", categories.get("commitment") or "",
            j.get("salaryDescription") or "", "https://jobs.lever.co/getwingapp", j.get("id")
        )
        if item:
            out.append(item)
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


def collect_project_sources():
    collected = []
    errors = []
    try:
        collected.extend(fetch_oneforma())
    except Exception as exc:
        errors.append({"source":"OneForma","error":str(exc)[:240]})
        print("OneForma feed failed: %s" % exc)
    for site, name in [("appen", "CrowdGen by Appen"), ("rws", "RWS TrainAI")]:
        try:
            batch = fetch_lever_projects(site, name)
            collected.extend(batch)
            print("%s: %s fresh Egypt-eligible projects" % (name, len(batch)))
        except Exception as exc:
            errors.append({"source":name,"error":str(exc)[:240]})
            print("%s feed failed: %s" % (name, exc))
    try:
        collected.extend(fetch_ai_training_projects())
    except Exception as exc:
        errors.append({"source":"AI training project feed","error":str(exc)[:240]})
        print("AI training project feed failed: %s" % exc)
    deduped = {}
    for item in collected:
        key = item["url"].split("?")[0].rstrip("/").lower()
        if key not in deduped or item.get("first_seen","") > deduped[key].get("first_seen",""):
            deduped[key] = item
    return list(deduped.values()), errors

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


def fetch_oneforma():
    endpoint = "https://www.oneforma.com/wp-json/wp/v2/job"
    rows = get_json(endpoint, {"per_page": 100, "orderby": "date", "order": "desc"})
    out = []
    if not isinstance(rows, list):
        return out
    for j in rows:
        title_obj = j.get("title") or {}
        content_obj = j.get("content") or {}
        title = title_obj.get("rendered", "") if isinstance(title_obj, dict) else str(title_obj)
        body = content_obj.get("rendered", "") if isinstance(content_obj, dict) else str(content_obj)
        pub = iso_date(j.get("date_gmt") or j.get("date"))
        if not title or not pub or pub < NOW - dt.timedelta(days=7):
            continue
        meta = j.get("meta") or {}
        status = clean_text(first_value(meta, "status", "project_status", "job_status") or "").lower()
        if any(word in status for word in ("closed", "paused", "expired", "inactive")):
            continue
        ok, eligibility = allowed(title + " " + body, body)
        if not ok:
            continue
        location = "Egypt" if EGYPT.search(title + " " + body) else ("Africa" if AFRICA.search(title + " " + body) else "Worldwide")
        item = record("OneForma", title, "OneForma", j.get("link"), location, body, pub,
                      clean_text(first_value(meta, "domain", "category") or "AI / language project"),
                      "project / contract",
                      clean_text(first_value(meta, "pay", "rate", "salary") or ""),
                      "https://www.oneforma.com/projects/", j.get("id"))
        if item:
            item["first_seen"] = pub.isoformat()
            item["last_seen"] = NOW.isoformat()
            item["applicant_count"] = None
            item["competition_note"] = "Applicant count not disclosed"
            item["link_status"] = "source_feed"
            out.append(item)
    return out

def fetch_lever_projects(site, platform_name):
    rows = get_json("https://api.lever.co/v0/postings/" + site, {"mode": "json"})
    out = []
    if not isinstance(rows, list):
        return out
    for j in rows:
        title = j.get("text") or j.get("title") or ""
        categories = j.get("categories") or {}
        location = str(categories.get("location") or "")
        body = j.get("descriptionPlain") or j.get("description") or ""
        created = j.get("createdAt")
        pub = iso_date(created / 1000 if isinstance(created, (int, float)) and created > 100000000000 else created)
        if not title or not pub or pub < NOW - dt.timedelta(days=7):
            continue
        ok, eligibility = allowed(title + " " + location, body)
        if not ok:
            continue
        url = j.get("hostedUrl") or j.get("applyUrl")
        item = record(platform_name, title, platform_name, url, title + " " + location, body, pub,
                      categories.get("team") or "AI / language project",
                      categories.get("commitment") or "contract / project",
                      j.get("salaryDescription") or "",
                      "https://jobs.lever.co/" + site, j.get("id"))
        if item:
            item["candidate_required_location"] = location or ("Egypt" if EGYPT.search(title) else "Worldwide")
            item["first_seen"] = pub.isoformat()
            item["last_seen"] = NOW.isoformat()
            item["applicant_count"] = None
            item["competition_note"] = "Applicant count not disclosed"
            item["link_status"] = "source_feed"
            out.append(item)
    return out

def fetch_20four7va():
    """Read 20four7VA's public Breezy HR jobs feed, including all engagement types."""
    rows = get_json("https://20four7va.breezy.hr/json")
    if not isinstance(rows, list):
        raise ValueError("20four7VA public jobs feed did not return a JSON list")
    out = []
    for j in rows:
        if not isinstance(j, dict):
            continue
        title = j.get("name") or j.get("title") or ""
        url = j.get("url") or ""
        loc_parts = []
        location = j.get("location")
        if isinstance(location, dict):
            loc_parts.extend(str(v) for v in location.values() if v not in (None, "", [], {}))
        elif location:
            loc_parts.append(str(location))
        for loc in (j.get("locations") or []):
            if isinstance(loc, dict):
                loc_parts.extend(str(v) for v in loc.values() if v not in (None, "", [], {}))
            elif loc:
                loc_parts.append(str(loc))
        remote_details = str(j.get("remote_details") or "")
        # Breezy's unrestricted "remote" flag means worldwide; location-limited
        # remote roles still have to pass our explicit Egypt/Africa location check.
        if j.get("is_remote") and remote_details.strip().lower() == "remote":
            location_text = "Worldwide"
        else:
            location_text = " ".join(loc_parts)
            if remote_details:
                location_text += " " + remote_details
        job_type = j.get("type") or ""
        if isinstance(job_type, dict):
            job_type = job_type.get("name") or job_type.get("label") or ""
        job_type = re.sub(r"[^a-z0-9]+", "_", clean_text(job_type).lower()).strip("_")
        item = record(
            "20four7VA", title, "20four7VA", url, location_text, "",
            j.get("published_date") or j.get("publishedDate") or "",
            j.get("department") or "Virtual Assistance / Customer Support",
            job_type, j.get("salary") or "", "https://20four7va.breezy.hr/",
            j.get("id") or j.get("friendly_id")
        )
        if item:
            item["candidate_required_location"] = clean_text(location_text).strip()
            out.append(item)
    return out


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
        {"name":"Himalayas","url":"https://himalayas.app/","api":"https://himalayas.app/docs/remote-jobs-api","note":"Public API queried for Egypt; only source-confirmed Egypt, Africa, or worldwide eligibility is shown."},
        {"name":"Jobicy","url":"https://jobicy.com/","api":"https://jobicy.com/jobs-rss-feed","note":"Public worldwide remote-jobs API; only explicit Egypt, Africa, or worldwide locations are included."},
        {"name":"We Work Remotely","url":"https://weworkremotely.com/","api":"https://weworkremotely.com/remote-job-rss-feed","note":"Official public RSS; only explicit Egypt, Africa, or worldwide locations are included."},
        {"name":"Remote OK","url":"https://remoteok.com/","api":"https://remoteok.com/api","note":"Free public JSON feed. Remote OK is credited and every listing links to its original post; explicit eligible locations only."},
        {"name":"Remotive","url":"https://remotive.com/","api":"https://remotive.com/api/remote-jobs","note":"Public API, refreshed once daily (within its recommended limit of 4 requests/day); 24-hour feed delay. Direct Remotive job/source links are retained."},
        {"name":"Working Nomads","url":"https://www.workingnomads.com/jobs","api":"https://www.workingnomads.com/api/exposed_jobs/","note":"Public jobs feed linked from its site API page; only explicit Egypt, Africa, or worldwide locations are included."},
        {"name":"Wing Assistant","url":"https://jobs.lever.co/getwingapp","api":"https://api.lever.co/v0/postings/getwingapp?mode=json","note":"Public Lever postings feed from the source listed in the supplied sheet; only explicit eligible locations are included."},
        {"name":"20four7VA","url":"https://20four7va.breezy.hr/","api":"https://20four7va.breezy.hr/json","note":"Public Breezy HR feed checked daily. Includes part-time, full-time, contract, temporary and other openings; only Egypt, Africa, or unrestricted worldwide remote eligibility is included."}
    ]
    collected, errors = [], []
    project_jobs = []
    for name, fn in [
        ("Himalayas", fetch_himalayas),
        ("Jobicy", fetch_jobicy),
        ("We Work Remotely", fetch_wwr),
        ("Remote OK", fetch_remoteok),
        ("Remotive", fetch_remotive),
        ("Working Nomads", fetch_working_nomads),
        ("Wing Assistant", fetch_wing_assistant),
        ("20four7VA", fetch_20four7va)
    ]:
        try:
            batch = fn()
            collected.extend(batch)
            print("%s: %s eligible listings" % (name, len(batch)))
        except Exception as exc:
            errors.append({"source":name,"error":str(exc)[:240]})
            print("%s failed: %s" % (name, exc))
    project_jobs, project_errors = collect_project_sources()
    errors.extend(project_errors)
    print("All project feeds: %s fresh Egypt-eligible listings" % len(project_jobs))
    deduped = {}
    for job in collected:
        canonical_url = job["url"].split("?")[0].rstrip("/").lower()
        signature_text = " ".join([
            clean_text(job.get("company_name", "")).lower(),
            clean_text(job.get("title", "")).lower(),
            clean_text(job.get("candidate_required_location", "")).lower()
        ])
        signature = re.sub(r"[^a-z0-9]+", " ", signature_text).strip()
        key = "listing:" + signature if signature else "url:" + canonical_url
        current = deduped.get(key)
        if current is None or (job.get("publication_date", "") > current.get("publication_date", "")):
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
