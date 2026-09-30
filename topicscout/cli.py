"""topicscout run | profiles | doctor.

Results go to stdout (a summary line, or JSON with --json); progress and
errors go to stderr, so a script or an agent can read stdout as is.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .core import Profile, _stderr, builtin_profiles, rate_limit, run


def _run(args) -> int:
    try:
        profile = Profile.load(args.profile)
    except (FileNotFoundError, ValueError, KeyError) as e:
        _stderr(f"topicscout: {e}")
        return 2
    out = Path(args.out or f"scout-{profile.name}")
    log = (lambda m: None) if args.quiet else _stderr
    if not os.environ.get("GITHUB_TOKEN"):
        log("no GITHUB_TOKEN: searches paced to 10 per minute, READMEs capped at 40")
    s = run(profile, out, min_stars=args.min_stars, days=args.days, readme_max=args.readme_max,
            known_from=args.known_from, log=log)
    if args.json:
        print(json.dumps({**s, "out": str(out)}, indent=2, ensure_ascii=False))
    else:
        print(f"{s['found']} found, {s['kept']} kept, {len(s['new'])} new -> {out / 'new.md'}")
    if s["stopped"]:
        _stderr(f"stopped early: {s['stopped']}")
    return 0


def _profiles(args) -> int:
    ps = builtin_profiles()
    if args.json:
        print(json.dumps(ps, indent=2))
    else:
        for name, desc in ps.items():
            print(f"{name:20} {desc}")
    return 0


def _doctor(args) -> int:
    token = bool(os.environ.get("GITHUB_TOKEN"))
    report = {"version": __version__, "token": token}
    try:
        res = rate_limit()
    except Exception as e:  # network, bad token: say it, don't crash
        report["error"] = str(e)
    else:
        for key in ("search", "core"):
            r = res[key]
            reset = datetime.fromtimestamp(r["reset"], timezone.utc).strftime("%H:%M:%S UTC")
            report[key] = {"remaining": r["remaining"], "limit": r["limit"], "reset": reset}
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"topicscout {__version__}")
        print(f"GITHUB_TOKEN: {'set' if token else 'not set (works, but slower and READMEs capped)'}")
        if "error" in report:
            print(f"GitHub: unreachable ({report['error']})")
        else:
            for key, label in (("search", "search API"), ("core", "READMEs (core API)")):
                r = report[key]
                print(f"{label}: {r['remaining']}/{r['limit']} left, resets {r['reset']}")
    return 1 if "error" in report else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="topicscout",
                                 description="Find the GitHub repos on a topic, and only the new ones next time.")
    ap.add_argument("--version", action="version", version=f"topicscout {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="search GitHub with a profile and list what is new")
    r.add_argument("profile", help="built-in profile name (see `topicscout profiles`) or path to a JSON profile")
    r.add_argument("--out", help="state folder: candidates.json, new.md, known.txt (default scout-<profile>)")
    r.add_argument("--min-stars", type=int, help="override the profile's min_stars")
    r.add_argument("--days", type=int, help="override the profile's days: drop repos with no push in this many days")
    r.add_argument("--readme-max", type=int, help="READMEs read per run (default 40, 300 with GITHUB_TOKEN)")
    r.add_argument("--known-from", action="append", default=[], metavar="GLOB",
                   help="files whose first 20 lines name repos you already have (repeatable)")
    r.add_argument("--json", action="store_true", help="print the summary and the new repos as JSON")
    r.add_argument("-q", "--quiet", action="store_true", help="no progress on stderr")
    r.set_defaults(func=_run)

    p = sub.add_parser("profiles", help="list the built-in profiles")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_profiles)

    d = sub.add_parser("doctor", help="token and GitHub quota")
    d.add_argument("--json", action="store_true")
    d.set_defaults(func=_doctor)

    args = ap.parse_args(argv)
    return args.func(args)
