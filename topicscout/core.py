"""Search, filter, remember: the whole scout, with no dependencies.

A profile says what to look for (GitHub search queries), what a real hit
must say about itself (require), what to drop (exclude) and how to guess
the interface from the README. The state is kept across runs, so new.md
holds only what appeared since the last one.
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

API = "https://api.github.com"
GITHUB_URL = re.compile(r"github\.com/([\w.-]+)/([\w.-]+)")


class RateLimited(Exception):
    pass


def fetch_github(url: str, accept: str) -> tuple[int, dict, str]:
    """GET with the optional GITHUB_TOKEN; returns (status, lower-case headers, body)."""
    headers = {"Accept": accept, "User-Agent": "topicscout", "X-GitHub-Api-Version": "2022-11-28"}
    if os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read().decode("utf-8", "replace")


def _stderr(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- profiles

PROFILES = Path(__file__).resolve().parent / "profiles"


def builtin_profiles() -> dict[str, str]:
    """name -> description of the profiles shipped with the package."""
    out = {}
    for p in sorted(PROFILES.glob("*.json")):
        out[p.stem] = json.loads(p.read_text(encoding="utf-8")).get("description", "")
    return out


class Profile:
    """A compiled profile: queries, require/exclude patterns, interface guesses."""

    def __init__(self, data: dict):
        self.name = data.get("name", "profile")
        self.description = data.get("description", "")
        self.queries = list(data["queries"])
        if not self.queries:
            raise ValueError("a profile needs at least one query")
        self.exclude = re.compile(data["exclude"]) if data.get("exclude") else None
        # own_words: the pattern must be in the repo's name or description, not only in a topic
        self.require = [(re.compile(r["pattern"]), bool(r.get("own_words"))) for r in data.get("require", [])]
        self.interfaces = [(n, re.compile(p)) for n, p in data.get("interfaces", [])]
        self.min_stars = int(data.get("min_stars", 10))
        self.days = int(data.get("days", 90))
        self.require_language = bool(data.get("require_language", True))

    @classmethod
    def load(cls, name_or_path: str) -> "Profile":
        path = Path(name_or_path)
        if not path.is_file():
            path = PROFILES / f"{name_or_path}.json"
        if not path.is_file():
            raise FileNotFoundError(f"no profile {name_or_path!r}: not a file, and built-in ones are "
                                    f"{', '.join(builtin_profiles())}")
        data = json.loads(path.read_text(encoding="utf-8"))
        data.setdefault("name", path.stem)
        return cls(data)


# ---------------------------------------------------------------- search

def _get(fetch, url, accept, sleep):
    """One request; waits out a short rate limit once, raises RateLimited on a long one."""
    for attempt in (0, 1):
        status, headers, body = fetch(url, accept)
        limited = status == 429 or (status == 403 and (headers.get("x-ratelimit-remaining") == "0"
                                                       or "retry-after" in headers))
        if not limited:
            return status, body
        if "retry-after" in headers:
            wait = int(headers["retry-after"])
        else:
            wait = int(headers.get("x-ratelimit-reset", "0")) - int(time.time())
        if attempt or wait > 70:
            raise RateLimited(f"GitHub rate limit, reset in {max(wait, 0)} s")
        sleep(max(wait, 0) + 1)
    raise AssertionError("unreachable")


def search(fetch, queries, *, min_stars, days, today, sleep, pause=0.0, pages=3,
           log=lambda m: None) -> tuple[dict, str | None]:
    """Run the queries; returns ({full_name: item}, reason it stopped early or None)."""
    since = (today - timedelta(days=days)).isoformat()
    found: dict[str, dict] = {}
    first = True
    for i, q in enumerate(queries, 1):
        full = f"{q} fork:false archived:false stars:>={min_stars} pushed:>={since}"
        for page in range(1, pages + 1):
            if not first and pause:
                sleep(pause)
            first = False
            url = f"{API}/search/repositories?" + urlencode(
                {"q": full, "sort": "stars", "order": "desc", "per_page": 100, "page": page})
            try:
                status, body = _get(fetch, url, "application/vnd.github+json", sleep)
            except RateLimited as e:
                return found, str(e)
            if status != 200:
                return found, f"search failed ({status}) on {q!r}: {body[:200]}"
            items = json.loads(body).get("items", [])
            for it in items:
                found.setdefault(it["full_name"], it)
            log(f"[{i}/{len(queries)}] {q} page {page}: {len(items)} ({len(found)} so far)")
            if len(items) < 100:
                break
    return found, None


def keep(item: dict, profile: Profile, *, min_stars: int, cutoff: date) -> bool:
    if item.get("fork") or item.get("archived"):
        return False
    if profile.require_language and not item.get("language"):
        return False
    if item.get("stargazers_count", 0) < min_stars:
        return False
    if datetime.fromisoformat(item["pushed_at"].replace("Z", "+00:00")).date() < cutoff:
        return False
    own = f"{item['full_name']} {item.get('description') or ''}"
    text = f"{own} {' '.join(item.get('topics') or [])}"
    if profile.exclude and profile.exclude.search(text):
        return False
    own_n, text_n = (s.replace("-", " ").replace("_", " ") for s in (own, text))
    return all(rx.search(own_n if own_words else text_n) for rx, own_words in profile.require)


def guess_interface(readme: str, profile: Profile) -> list[str]:
    return [name for name, rx in profile.interfaces if rx.search(readme)]


def known_repos(known_file: Path, known_from: list[str] = (), head: int = 20) -> set[str]:
    """owner/repo (lower case) from known.txt plus the GitHub URLs in the first
    `head` lines of the files matching the known_from globs."""
    known: set[str] = set()
    for pattern in known_from:
        for f in glob.glob(pattern, recursive=True):
            p = Path(f)
            if not p.is_file():
                continue
            top = "\n".join(p.read_text(encoding="utf-8", errors="replace").splitlines()[:head])
            for owner, name in GITHUB_URL.findall(top):
                name = name.rstrip(".").removesuffix(".git")
                known.add(f"{owner}/{name}".lower())
    if known_file.is_file():
        for line in known_file.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                known.add(line.lower())
    return known


# ---------------------------------------------------------------- run

def _record(item: dict) -> dict:
    return {"full_name": item["full_name"], "url": item["html_url"], "stars": item["stargazers_count"],
            "pushed": item["pushed_at"][:10], "license": (item.get("license") or {}).get("spdx_id"),
            "language": item.get("language"), "description": item.get("description") or "",
            "topics": item.get("topics") or []}


def _new_md(new: list[dict], profile: Profile, today: date, stopped: str | None) -> str:
    lines = [f"# New repos for {profile.name}, {today.isoformat()}", ""]
    if stopped:
        lines += [f"> Stopped early: {stopped}. The list is partial.", ""]
    if not new:
        return "\n".join(lines + ["Nothing new since the last run.", ""])
    lines += [f"{len(new)} new.", "", "| repo | stars | last push | language | license | interface | description |",
              "|---|---:|---|---|---|---|---|"]
    for r in new:
        iface = ", ".join(r["interface"]) if r.get("interface") else ("?" if r.get("interface") is None else "-")
        desc = r["description"].replace("|", "\\|").replace("\n", " ")[:160]
        lines.append(f"| [{r['full_name']}]({r['url']}) | {r['stars']} | {r['pushed']} | {r['language'] or '-'} | "
                     f"{r['license'] or '-'} | {iface} | {desc} |")
    return "\n".join(lines + [""])


def run(profile: Profile, out: Path, fetch=None, *, today: date | None = None, sleep=None,
        min_stars: int | None = None, days: int | None = None, readme_max: int | None = None,
        pause: float | None = None, known_from: list[str] = (), log=lambda m: None) -> dict:
    fetch = fetch or fetch_github
    sleep = sleep or time.sleep
    today = today or datetime.now(timezone.utc).date()
    min_stars = profile.min_stars if min_stars is None else min_stars
    days = profile.days if days is None else days
    token = bool(os.environ.get("GITHUB_TOKEN"))
    if pause is None:
        pause = 2.0 if token else 7.0  # search API: 30/min with a token, 10/min without
    if readme_max is None:
        readme_max = 300 if token else 40
    out.mkdir(parents=True, exist_ok=True)
    state_file = out / "candidates.json"
    state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.is_file() else {"repos": {}}
    repos: dict[str, dict] = state["repos"]
    known = known_repos(out / "known.txt", known_from)

    found, stopped = search(fetch, profile.queries, min_stars=min_stars, days=days, today=today,
                            sleep=sleep, pause=pause, log=log)
    cutoff = today - timedelta(days=days)
    new_names = []
    for name, item in found.items():
        if not keep(item, profile, min_stars=min_stars, cutoff=cutoff):
            continue
        rec = _record(item)
        old = repos.get(name)
        rec["first_seen"] = old["first_seen"] if old else today.isoformat()
        rec["last_seen"] = today.isoformat()
        rec["interface"] = old.get("interface") if old else None
        if name.lower() in known:
            rec["status"] = "known"
        elif old:
            rec["status"] = "seen"
        else:
            rec["status"] = "new"
            new_names.append(name)
        repos[name] = rec

    # READMEs: first sightings first, then older ones never read (cap reached last time)
    if profile.interfaces:
        todo = new_names + [n for n, r in repos.items() if r["status"] == "seen" and r.get("interface") is None]
        for name in todo[:readme_max]:
            try:
                status, body = _get(fetch, f"{API}/repos/{name}/readme", "application/vnd.github.raw", sleep)
            except RateLimited as e:
                stopped = stopped or f"{e} (while reading READMEs)"
                break
            repos[name]["interface"] = guess_interface(body, profile) if status == 200 else []
        log(f"READMEs read: {min(len(todo), readme_max)} of {len(todo)}")

    state = {"profile": profile.name, "updated": today.isoformat(),
             "repos": dict(sorted(repos.items(), key=lambda kv: kv[0].lower()))}
    state_file.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    new = sorted((repos[n] for n in new_names), key=lambda r: -r["stars"])
    (out / "new.md").write_text(_new_md(new, profile, today, stopped), encoding="utf-8")
    return {"profile": profile.name, "found": len(found),
            "kept": sum(1 for r in repos.values() if r["last_seen"] == today.isoformat()),
            "new": new, "stopped": stopped}


def rate_limit(fetch=None) -> dict:
    """GitHub's own view of the quota; this call does not count against it."""
    fetch = fetch or fetch_github
    status, _, body = fetch(f"{API}/rate_limit", "application/vnd.github+json")
    if status != 200:
        raise RuntimeError(f"rate_limit failed ({status}): {body[:200]}")
    return json.loads(body)["resources"]
