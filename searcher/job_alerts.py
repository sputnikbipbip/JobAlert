#!/usr/bin/env python3
"""Collect recent jobs into a static feed and deliver a durable Telegram outbox."""

import argparse
import html
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

UTC = timezone.utc
MAX_AGE_DAYS = 14
MAX_DISPLAY = 40
MAX_ALERTS = 20
SOURCES = ("Remotive", "Arbeitnow", "Jobicy")
HEADERS = {"User-Agent": "PersonalJobAlerts/1.0", "Accept": "application/json"}


def now():
    return datetime.now(UTC)


def stamp(value):
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_date(value):
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, UTC)
    result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return result.replace(tzinfo=UTC) if result.tzinfo is None else result.astimezone(UTC)


def text(value):
    value = html.unescape(re.sub(r"<[^>]*>", " ", str(value or "")))
    return " ".join(value.split())


def normal(value):
    return "".join(c for c in unicodedata.normalize("NFKD", value.lower()) if not unicodedata.combining(c))


def contains(value, pattern):
    return re.search(pattern, normal(value), re.I) is not None


def fetch_json(url, *, data=None):
    headers = dict(HEADERS)
    if data is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(data).encode()
    request = Request(url, data=data, headers=headers)
    with urlopen(request, timeout=40) as response:
        return json.load(response)


def safe_url(value):
    parsed = urlparse(str(value))
    return (parsed.scheme == "https" and not parsed.username and not parsed.password
            and parsed.netloc in ("remotive.com", "www.arbeitnow.com", "arbeitnow.com", "jobicy.com") and len(value) <= 512)


def classify(title, description, tags, kind):
    title = normal(title)
    context = normal(" ".join((description, tags)))
    internship = contains(title + " " + kind, r"\b(intern(ship)?|estagio|trainee|graduate)\b")
    senior = contains(title, r"\b(senior|sr\.?|staff|principal|lead|director|head|manager|architect)\b")
    engineer = contains(title, r"\b(engineer|developer|programmer|engenheiro|desenvolvedor|engineering)\b")
    ai_title = contains(title, r"\b(ai|ml|llm|machine learning|artificial intelligence|generative ai|genai|rag)\b")
    ai_context = contains(context, r"\b(llm|machine learning|artificial intelligence|generative ai|retrieval.augmented generation|rag|mcp)\b")
    dotnet_title = contains(title, r"(?<!\w)(\.net|dotnet|c#|asp\.net)(?!\w)")
    dotnet_context = contains(context, r"(?<!\w)(\.net|dotnet|c#|asp\.net)(?!\w)")
    if senior:
        return None
    if internship:
        return "AI internship" if ai_title else None
    if engineer and (ai_title or (contains(title, r"\b(backend|back-end|software)\b") and ai_context)):
        return "AI engineering"
    if engineer and (dotnet_title or (contains(title, r"\b(backend|back-end|software|full.?stack)\b") and dotnet_context)):
        if contains(title, r"\b(junior|jr\.?|entry.level)\b"):
            return None
        return ".NET engineering"
    return None


def location_match(source, location, remote):
    loc = normal(location)
    city = contains(loc, r"\b(viseu|aveiro|porto)\b")
    # City names can occur in other countries; require Portugal or an unqualified city.
    local = city and (contains(loc, r"\b(portugal|portuguesa)\b") or
                      re.fullmatch(r"\s*(viseu|aveiro|porto)\s*", loc) is not None)
    if source == "Arbeitnow" and not remote:
        return "Local (hybrid unconfirmed)" if local else None
    if source == "Arbeitnow" and remote and local:
        return "Remote / local"
    # Avoid assuming a remote job is open to Portugal when its location is missing or ambiguous.
    allowed = contains(loc, r"\b(worldwide|anywhere|global|europe|european union|emea|portugal|eu)\b")
    exclusions = contains(loc, r"\b(except|excluding|not in|no applicants from)\b")
    return "Remote (check eligibility)" if allowed and not exclusions else None


def normalize_job(source, raw, current):
    try:
        if source == "Remotive":
            identity, published = raw["id"], parse_date(raw["publication_date"])
            location, remote = text(raw.get("candidate_required_location")), True
            kind = text(raw.get("job_type"))
        elif source == "Arbeitnow":
            identity, published = raw["slug"], parse_date(raw["created_at"])
            location, remote = text(raw.get("location")), raw.get("remote") is True
            kind = " ".join(raw.get("job_types") or [])
        else:
            identity, published = raw["id"], parse_date(raw["pubDate"])
            location, remote = text(raw.get("jobGeo")), True
            kind = " ".join(raw.get("jobType") or [])
        title = text(raw["jobTitle"] if source == "Jobicy" else raw["title"])
        company = text(raw["companyName"] if source == "Jobicy" else raw["company_name"])
        url = str(raw["url"])
        if not identity or not title or not company or not safe_url(url):
            return None
        if published < current - timedelta(days=MAX_AGE_DAYS) or published > current + timedelta(hours=1):
            return None
        category = classify(title, text(raw.get("jobDescription") if source == "Jobicy" else raw.get("description")),
                            " ".join(raw.get("tags") or []), kind)
        if source == "Jobicy" and contains(text(raw.get("jobLevel")), r"\b(senior|staff|principal|lead|director|manager)\b"):
            return None
        place = location_match(source, location, remote)
        if not category or not place:
            return None
        return {"id": f"{source.lower()}:{identity}", "title": title[:200], "company": company[:160],
                "location": location[:200], "work_mode": place, "category": category,
                "published_at": stamp(published), "url": url, "source": source}
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def get_sources(current):
    results, errors = {}, {}
    for source in SOURCES:
        try:
            if source == "Remotive":
                payload = fetch_json("https://remotive.com/api/remote-jobs?category=software-dev")
                raw_jobs = payload["jobs"]
            elif source == "Arbeitnow":
                raw_jobs = []
                # The API is ordered newest first. Bound requests to avoid abusing the free API.
                for page in range(1, 6):
                    payload = fetch_json(f"https://www.arbeitnow.com/api/job-board-api?page={page}")
                    page_jobs = payload["data"]
                    if not isinstance(page_jobs, list):
                        raise ValueError("unexpected jobs response")
                    raw_jobs.extend(page_jobs)
                    if not page_jobs or not payload.get("links", {}).get("next"):
                        break
                    dates = [parse_date(job["created_at"]) for job in page_jobs if job.get("created_at")]
                    if dates and min(dates) < current - timedelta(days=MAX_AGE_DAYS):
                        break
            else:
                raw_jobs = []
                # Jobicy returns only seven days. Three geography filters include Europe,
                # Portugal and broader EMEA; IDs are deduplicated below.
                for geo in ("europe", "portugal", "emea"):
                    cursor = None
                    for _ in range(5):
                        params = {"count": 100, "geo": geo, "industry": "engineering"}
                        if cursor:
                            params["cursor"] = cursor
                        payload = fetch_json("https://jobicy.com/api/jobs?" + urlencode(params))
                        if payload.get("success") is not True or not isinstance(payload.get("jobs"), list):
                            raise ValueError("unexpected jobs response")
                        raw_jobs.extend(payload["jobs"])
                        cursor = payload.get("nextCursor")
                        if not cursor:
                            break
            if not isinstance(raw_jobs, list):
                raise ValueError("unexpected jobs response")
            results[source] = [job for raw in raw_jobs if (job := normalize_job(source, raw, current))]
            print(f"{source}: {len(results[source])} matches from {len(raw_jobs)} listings")
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, KeyError, TypeError) as exc:
            errors[source] = str(exc)
            print(f"{source} failed: {exc}", file=sys.stderr)
    return results, errors


def read_json(path, default):
    if not path.exists():
        return default
    with path.open(encoding="utf-8") as file:
        return json.load(file)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def scan(directory):
    current = now()
    feed_path, state_path = directory / "jobs.json", directory / "state.json"
    previous = read_json(feed_path, {})
    state = read_json(state_path, {"sent": {}, "pending": []})
    results, errors = get_sources(current)
    # Preserve the last successful source snapshot on failures, rather than silently deleting it.
    combined = [job for jobs in results.values() for job in jobs]
    combined.extend(job for job in previous.get("jobs", []) if job.get("source") in errors
                    and parse_date(job["published_at"]) >= current - timedelta(days=MAX_AGE_DAYS))
    jobs = sorted({job["id"]: job for job in combined}.values(), key=lambda job: job["published_at"], reverse=True)
    sent = {key: value for key, value in state.get("sent", {}).items()
            if parse_date(value) >= current - timedelta(days=60)}
    current_ids = {job["id"] for job in jobs}
    pending = {job["id"]: job for job in state.get("pending", [])
               if job["id"] not in sent and parse_date(job["published_at"]) >= current - timedelta(days=MAX_AGE_DAYS)
               and (job["source"] in errors or job["id"] in current_ids)}
    for job in jobs:
        if job["id"] not in sent:
            pending[job["id"]] = job
    state = {"sent": sent, "pending": list(pending.values())}
    source_status = {source: {"ok": source in results, "error": errors.get(source)} for source in SOURCES}
    feed = {"generated_at": stamp(current), "sources": source_status, "jobs": jobs[:MAX_DISPLAY]}
    # Bound encoded JSON, not just character count, to fit the ESP32's download limit.
    while feed["jobs"] and len(json.dumps(feed, ensure_ascii=False, indent=2).encode("utf-8")) > 48 * 1024:
        feed["jobs"].pop()
    write_json(state_path, state)
    write_json(feed_path, feed)
    print(f"Feed: {len(jobs)} matches; {len(pending)} pending alerts")
    return 0 if results else 1


def deliver(directory, token, chat_id):
    if not token or not chat_id:
        print("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required", file=sys.stderr)
        return 1
    path = directory / "state.json"
    state = read_json(path, {"sent": {}, "pending": []})
    pending = sorted(state["pending"], key=lambda job: job["published_at"], reverse=True)
    jobicy_ids = [str(job["id"]).split(":", 1)[1] for job in pending if job.get("source") == "Jobicy"]
    try:
        for offset in range(0, len(jobicy_ids), 100):
            payload = fetch_json("https://jobicy.com/api/jobs/status?" + urlencode({"ids": ",".join(jobicy_ids[offset:offset + 100])}))
            if payload.get("success") is not True or not isinstance(payload.get("jobs"), list):
                raise ValueError("unexpected Jobicy status response")
            active = {str(item["id"]) for item in payload["jobs"] if item.get("status") == "active"}
            to_remove = {"jobicy:" + job_id for job_id in jobicy_ids[offset:offset + 100] if job_id not in active}
            state["pending"] = [job for job in state["pending"] if job["id"] not in to_remove]
        write_json(path, state)
        pending = sorted(state["pending"], key=lambda job: job["published_at"], reverse=True)
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, KeyError) as exc:
        print(f"Jobicy status check failed ({type(exc).__name__}); pending alerts retained", file=sys.stderr)
        return 1
    for job in pending[:MAX_ALERTS]:
        message = (f"{job['category']}\n{job['title']} — {job['company']}\n"
                   f"{job['location']} · {job['work_mode']}\n"
                   f"Posted: {job['published_at'][:10]} · Source: {job['source']}\n{job['url']}")
        try:
            response = fetch_json(f"https://api.telegram.org/bot{token}/sendMessage", data={
                "chat_id": chat_id, "text": message[:4000], "disable_web_page_preview": True})
            if not response.get("ok"):
                raise ValueError("Telegram rejected the message")
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            # Do not log request URLs: the Telegram token is embedded in the URL.
            print(f"Telegram delivery failed ({type(exc).__name__}); pending alerts retained", file=sys.stderr)
            return 1
        state["sent"][job["id"]] = stamp(now())
        state["pending"] = [item for item in state["pending"] if item["id"] != job["id"]]
        write_json(path, state)
        time.sleep(0.1)
    print(f"Sent {min(len(pending), MAX_ALERTS)} alerts; {len(state['pending'])} remain")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("scan", "deliver"))
    parser.add_argument("--directory", type=Path, default=Path("public"))
    args = parser.parse_args()
    if args.command == "scan":
        return scan(args.directory)
    return deliver(args.directory, os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID"))


if __name__ == "__main__":
    sys.exit(main())
