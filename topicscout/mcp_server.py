"""MCP face of topicscout: run a scout, read what it found, mark repos as known.

    pip install "topicscout[mcp]"
    topicscout mcp          # stdio server

The state of each profile lives in $TOPICSCOUT_HOME/<profile> (default
~/.topicscout/<profile>), or in the folder given as `out`, the same folder
the CLI's --out takes. The tools other than scout_run and doctor never touch
the network: they read the state of the last run.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import core
from .core import Profile

AGENT_LIMIT = 50  # rows per answer, sized for an agent's context
STATUSES = ("new", "seen", "known")


def home() -> Path:
    return Path(os.environ.get("TOPICSCOUT_HOME") or Path.home() / ".topicscout")


def state_dir(profile: str, out: str | None = None) -> Path:
    if out:
        return Path(out)
    name = Profile.load(profile).name if Path(profile).is_file() else profile
    return home() / name


def _state(folder: Path) -> dict:
    f = folder / "candidates.json"
    if not f.is_file():
        raise FileNotFoundError(f"no state in {folder}: run scout_run first")
    return json.loads(f.read_text(encoding="utf-8"))


def _short(r: dict) -> dict:
    return {k: r.get(k) for k in ("full_name", "stars", "pushed", "language", "license",
                                  "interface", "description", "status", "first_seen")}


# ---------------------------------------------------------------- tools, plain functions

def list_profiles() -> dict:
    return {"profiles": core.builtin_profiles(), "home": str(home())}


def scout_run(profile: str, out: str | None = None, min_stars: int | None = None, days: int | None = None,
              readme_max: int | None = None, known_from: list[str] | None = None, fetch=None) -> dict:
    p = Profile.load(profile)
    folder = Path(out) if out else home() / p.name
    s = core.run(p, folder, fetch, min_stars=min_stars, days=days, readme_max=readme_max,
                 known_from=known_from or [])
    return {**s, "new": [_short(r) for r in s["new"]], "out": str(folder),
            "token": bool(os.environ.get("GITHUB_TOKEN"))}


def scout_candidates(profile: str, out: str | None = None, status: str | None = None,
                     text: str | None = None, min_stars: int = 0, limit: int = AGENT_LIMIT) -> dict:
    if status and status not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}")
    st = _state(state_dir(profile, out))
    rows = list(st["repos"].values())
    if status:
        rows = [r for r in rows if r["status"] == status]
    if text:
        t = text.lower()
        rows = [r for r in rows if t in f"{r['full_name']} {r['description']} {' '.join(r['topics'])}".lower()]
    rows = sorted((r for r in rows if r["stars"] >= min_stars), key=lambda r: -r["stars"])
    return {"updated": st.get("updated"), "total": len(rows), "truncated": len(rows) > limit,
            "repos": [_short(r) for r in rows[:limit]]}


def scout_mark_known(profile: str, repo: str, note: str = "", out: str | None = None) -> dict:
    """Add owner/repo to known.txt so no later run lists it as new."""
    repo = repo.strip().removeprefix("https://github.com/").strip("/")
    if repo.count("/") != 1:
        raise ValueError("repo must be owner/name")
    folder = state_dir(profile, out)
    folder.mkdir(parents=True, exist_ok=True)
    known = folder / "known.txt"
    already = core.known_repos(known)
    if repo.lower() not in already:
        line = f"{repo}  # {note}" if note else repo
        with known.open("a", encoding="utf-8") as f:
            f.write(line.replace("\n", " ") + "\n")
    state_file = folder / "candidates.json"
    if state_file.is_file():
        st = json.loads(state_file.read_text(encoding="utf-8"))
        for name, r in st["repos"].items():
            if name.lower() == repo.lower():
                r["status"] = "known"
        state_file.write_text(json.dumps(st, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return {"repo": repo, "added": repo.lower() not in already, "known_file": str(known)}


def doctor(fetch=None) -> dict:
    report = {"token": bool(os.environ.get("GITHUB_TOKEN")), "home": str(home())}
    try:
        res = core.rate_limit(fetch)
    except Exception as e:  # network, bad token: say it
        report["error"] = str(e)
    else:
        report.update({k: {"remaining": res[k]["remaining"], "limit": res[k]["limit"], "reset": res[k]["reset"]}
                       for k in ("search", "core")})
    return report


# ---------------------------------------------------------------- server

def _json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


def build_server():
    try:  # mcp 2.x
        from mcp.server.mcpserver import MCPServer
    except ImportError:  # mcp 1.x, same class under its old name
        from mcp.server.fastmcp import FastMCP as MCPServer

    mcp = MCPServer("topicscout", instructions=(
        "Finds GitHub repositories on a topic and remembers them across runs. A profile "
        "(list_profiles) says what to search. scout_run searches GitHub and is slow: minutes "
        "without GITHUB_TOKEN; run it once, then read with scout_candidates (status='new' "
        "for what the last run found). scout_mark_known drops a repo from later runs."))

    @mcp.tool(name="list_profiles")
    def list_profiles_tool() -> str:
        """Built-in profiles (name -> what they look for) and the state folder."""
        return _json(list_profiles())

    @mcp.tool(name="scout_run")
    def scout_run_tool(profile: str, out: str | None = None, min_stars: int | None = None,
                       days: int | None = None, readme_max: int | None = None,
                       known_from: list[str] | None = None) -> str:
        """Search GitHub with a profile (built-in name or path to a JSON profile) and
        return the repos that are new since the last run. Slow: about 7 s per search
        without GITHUB_TOKEN. `out` overrides the state folder; `known_from` are globs of
        files whose first 20 lines name repos already known. If `stopped` is set the
        run hit a rate limit and the list is partial."""
        return _json(scout_run(profile, out, min_stars, days, readme_max, known_from))

    @mcp.tool(name="scout_candidates")
    def scout_candidates_tool(profile: str, status: str | None = None, text: str | None = None,
                              min_stars: int = 0, limit: int = AGENT_LIMIT, out: str | None = None) -> str:
        """Repos seen by past runs, most stars first, without touching the network.
        status: new (found by the last run), seen, known. text: substring of name,
        description or topics."""
        return _json(scout_candidates(profile, out, status, text, min_stars, limit))

    @mcp.tool(name="scout_mark_known")
    def scout_mark_known_tool(profile: str, repo: str, note: str = "", out: str | None = None) -> str:
        """Mark owner/repo as known (already measured, rejected, not relevant): it goes in
        known.txt with the note, and later runs never list it as new."""
        return _json(scout_mark_known(profile, repo, note, out))

    @mcp.tool(name="doctor")
    def doctor_tool() -> str:
        """Whether GITHUB_TOKEN is set and how much GitHub quota is left (reset as epoch seconds)."""
        return _json(doctor())

    return mcp


def main() -> int:
    try:
        server = build_server()
    except ImportError:
        core._stderr('topicscout mcp needs the MCP SDK: pip install "topicscout[mcp]"')
        return 2
    server.run()
    return 0
