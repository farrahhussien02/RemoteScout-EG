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
            location = "Egypt" if any(str(x).lower() in ("egypt", "eg") for x in restrictions) else ("Worldwide" if not restrictions else ", ".join(restrictions))
            desc = j.get("description") or j.get("excerpt") or ""
            item = record("Himalayas", j.get("title"), j.get("companyName"), j.get("applicationLink"), location, desc, j.get("pubDate"), ", ".join(j.get("categories") or j.get("parentCategories") or []), j.get("employmentType"), "", "https://himalayas.app/", j.get("guid"), j.get("expiryDate"))
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
    for name, fn in [("Himalayas", fetch_himalayas), ("Jobicy", fetch_jobicy), ("We Work Remotely", fetch_wwr)]:
        try:
            batch = fn()
            collected.extend(batch)
            print("%s: %s eligible listings" % (name, len(batch)))
        except Exception as exc:
            errors.append({"source":name,"error":str(exc)[:240]})
            print("%s failed: %s" % (name, exc))
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
        "update_note": "Automatically refreshed daily. Listings are re-read from their sources; expired, missing, broken, and explicitly Egypt-excluding listings are removed.",
        "source_errors": errors
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    print("Saved %s verified listings to %s" % (len(checked), OUT))

if __name__ == "__main__":
    main()
