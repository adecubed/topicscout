"""topicscout with fake HTTP (no network)."""
from __future__ import annotations

import json
from datetime import date
from urllib.parse import parse_qs, urlparse

import pytest

from topicscout import cli, core
from topicscout.core import Profile

TODAY = date(2026, 9, 29)
MEM = Profile.load("ai-memory")


def repo(name, stars=50, pushed="2026-09-20T10:00:00Z", fork=False, archived=False,
         language="Python", description="Long-term memory for agents", topics=("agent-memory",)):
    return {"full_name": name, "html_url": f"https://github.com/{name}", "stargazers_count": stars,
            "pushed_at": pushed, "fork": fork, "archived": archived, "language": language,
            "description": description, "license": {"spdx_id": "MIT"}, "topics": list(topics)}


class FakeGitHub:
    """Every search query returns the same items; READMEs from a dict."""

    def __init__(self, items, readmes=None, limited_after=None):
        self.items, self.readmes = items, readmes or {}
        self.limited_after = limited_after
        self.searches, self.readme_calls = [], []

    def __call__(self, url, accept):
        path = urlparse(url).path
        if path == "/search/repositories":
            if self.limited_after is not None and len(self.searches) >= self.limited_after:
                return 403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "9999999999"}, "{}"
            self.searches.append(parse_qs(urlparse(url).query)["q"][0])
            return 200, {}, json.dumps({"items": self.items})
        if path.endswith("/readme"):
            name = path[len("/repos/"):-len("/readme")]
            self.readme_calls.append(name)
            if name in self.readmes:
                return 200, {}, self.readmes[name]
            return 404, {}, ""
        raise AssertionError(url)


def run(tmp_path, gh, profile=MEM, **kw):
    return core.run(profile, tmp_path / "scout", gh, today=TODAY, sleep=lambda s: None, **kw)


def load(tmp_path):
    return json.loads((tmp_path / "scout" / "candidates.json").read_text(encoding="utf-8"))["repos"]


def one_query(**extra):
    return Profile({"name": "t", "queries": ["topic:a"], **extra})


def test_queries_carry_the_filters():
    gh = FakeGitHub([])
    core.search(gh, ["topic:agent-memory"], min_stars=10, days=90, today=TODAY, sleep=lambda s: None)
    q = gh.searches[0]
    assert "topic:agent-memory" in q and "fork:false" in q and "archived:false" in q
    assert "stars:>=10" in q and "pushed:>=2026-07-01" in q


def test_ai_memory_drops_forks_archived_stale_small_lists(tmp_path):
    items = [repo("a/good"), repo("a/fork", fork=True), repo("a/old", archived=True),
             repo("a/stale", pushed="2026-01-01T00:00:00Z"), repo("a/tiny", stars=3),
             repo("a/nolang", language=None), repo("a/awesome-agent-memory"),
             repo("a/papers", description="A curated list of memory papers"),
             repo("a/db", description="Git for data, for agents", topics=("agent-memory", "database")),
             repo("a/claude-mem", description="Persistent context across sessions for every agent")]
    run(tmp_path, FakeGitHub(items))
    assert set(load(tmp_path)) == {"a/good", "a/claude-mem"}


def test_github_scrapers_profile():
    gs = Profile.load("github-scrapers")
    cutoff = date(2025, 9, 29)
    ok = repo("n/github-scraper", description="crawl GitHub web pages", topics=())
    spam = repo("x/OnlySnap", description="Scrape OnlyFans, a GitHub mirror", topics=())
    other = repo("y/web-scraper", description="scrape any website", topics=())
    assert core.keep(ok, gs, min_stars=5, cutoff=cutoff)
    assert not core.keep(spam, gs, min_stars=5, cutoff=cutoff)
    assert not core.keep(other, gs, min_stars=5, cutoff=cutoff)


def test_require_own_words_ignores_topics():
    p = one_query(require=[{"pattern": "(?i)memory", "own_words": True}])
    tagged = repo("a/db", description="Git for data", topics=("agent-memory",))
    said = repo("a/mem", description="memory for agents", topics=())
    cutoff = date(2026, 1, 1)
    assert not core.keep(tagged, p, min_stars=1, cutoff=cutoff)
    assert core.keep(said, p, min_stars=1, cutoff=cutoff)


def test_require_language_can_be_off():
    p = one_query(require_language=False)
    assert core.keep(repo("a/docs", language=None), p, min_stars=1, cutoff=date(2026, 1, 1))


def test_interface_guess_from_readme():
    g = lambda s: core.guess_interface(s, MEM)  # noqa: E731
    assert g("Add it to Claude: an MCP server (modelcontextprotocol)") == ["mcp"]
    assert g("pip install foo\n\ncurl http://localhost:8080/v1/memories") == ["pip", "rest"]
    assert g("npm install -g bar; docker compose up") == ["npm", "docker"]
    assert g("A Claude Code plugin: /plugin install x") == ["plugin"]
    assert g("") == []


def test_no_interfaces_no_readmes(tmp_path):
    gh = FakeGitHub([repo("a/one")])
    run(tmp_path, gh, profile=one_query())
    assert gh.readme_calls == [] and load(tmp_path)["a/one"]["interface"] is None


def test_known_from_files_and_known_txt(tmp_path):
    ad = tmp_path / "adapters"
    ad.mkdir()
    (ad / "x.py").write_text('"""Adapter for X (https://github.com/Owner/X-Mem)."""\n', encoding="utf-8")
    (ad / "late.py").write_text("\n" * 30 + "# https://github.com/not/this\n", encoding="utf-8")
    out = tmp_path / "scout"
    out.mkdir()
    (out / "known.txt").write_text("# rejected\nother/thing\n", encoding="utf-8")
    known_from = [str(ad / "*.py")]
    assert core.known_repos(out / "known.txt", known_from) == {"owner/x-mem", "other/thing"}
    run(tmp_path, FakeGitHub([repo("owner/X-Mem"), repo("other/thing"), repo("new/one")]), known_from=known_from)
    repos = load(tmp_path)
    assert repos["owner/X-Mem"]["status"] == "known" and repos["new/one"]["status"] == "new"
    md = (out / "new.md").read_text(encoding="utf-8")
    assert "new/one" in md and "owner/X-Mem" not in md


def test_state_across_runs(tmp_path):
    gh = FakeGitHub([repo("a/one", stars=20)], readmes={"a/one": "pip install one"})
    first = run(tmp_path, gh)
    assert [r["full_name"] for r in first["new"]] == ["a/one"] and gh.readme_calls == ["a/one"]
    gh.items = [repo("a/one", stars=25), repo("b/two", stars=90)]
    second = run(tmp_path, gh)
    assert [r["full_name"] for r in second["new"]] == ["b/two"]
    assert gh.readme_calls == ["a/one", "b/two"]  # README only for first sightings
    repos = load(tmp_path)
    assert repos["a/one"]["status"] == "seen" and repos["a/one"]["stars"] == 25
    assert repos["a/one"]["first_seen"] == "2026-09-29" and repos["a/one"]["interface"] == ["pip"]


def test_new_md_most_stars_first(tmp_path):
    run(tmp_path, FakeGitHub([repo("a/small", stars=11), repo("a/big", stars=900)]))
    md = (tmp_path / "scout" / "new.md").read_text(encoding="utf-8")
    assert md.startswith("# New repos for ai-memory") and md.index("a/big") < md.index("a/small")


def test_readme_cap(tmp_path):
    gh = FakeGitHub([repo(f"a/r{i}") for i in range(5)])
    run(tmp_path, gh, readme_max=2)
    assert len(gh.readme_calls) == 2


def test_rate_limit_saves_what_was_found(tmp_path):
    gh = FakeGitHub([repo("a/one")], limited_after=1)
    p = one_query()
    p.queries = ["topic:a", "topic:b", "topic:c"]
    summary = run(tmp_path, gh, profile=p)
    assert summary["stopped"] and "rate limit" in summary["stopped"]
    assert set(load(tmp_path)) == {"a/one"}


def test_profile_from_path_and_unknown(tmp_path):
    f = tmp_path / "mine.json"
    f.write_text(json.dumps({"queries": ["topic:x"]}), encoding="utf-8")
    p = Profile.load(str(f))
    assert p.name == "mine" and p.min_stars == 10 and p.days == 90
    with pytest.raises(FileNotFoundError):
        Profile.load("no-such-profile")
    with pytest.raises(ValueError):
        Profile({"queries": []})


def test_builtin_profiles_all_load():
    names = core.builtin_profiles()
    assert {"ai-memory", "github-scrapers"} <= set(names)
    for n in names:
        Profile.load(n)


def test_cli_run_json(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(core, "fetch_github", FakeGitHub([repo("a/one")]))
    monkeypatch.setattr(core.time, "sleep", lambda s: None)
    rc = cli.main(["run", "ai-memory", "--out", str(tmp_path / "o"), "--json", "-q"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["profile"] == "ai-memory" and [r["full_name"] for r in out["new"]] == ["a/one"]


def test_cli_unknown_profile(capsys):
    assert cli.main(["run", "nope"]) == 2
    assert "no profile" in capsys.readouterr().err
