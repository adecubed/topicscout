"""The MCP tools as plain functions (fake HTTP), and the real server over stdio."""
from __future__ import annotations

import asyncio
import json
import sys

import pytest

from topicscout import mcp_server as ms
from test_topicscout import FakeGitHub, repo


@pytest.fixture(autouse=True)
def scout_home(tmp_path, monkeypatch):
    monkeypatch.setenv("TOPICSCOUT_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(ms.core.time, "sleep", lambda s: None)
    return tmp_path / "home"


def test_list_profiles(scout_home):
    out = ms.list_profiles()
    assert "ai-memory" in out["profiles"] and out["home"] == str(scout_home)


def test_run_then_candidates(scout_home):
    gh = FakeGitHub([repo("a/big", stars=900), repo("a/small", stars=20)], readmes={"a/big": "pip install big"})
    s = ms.scout_run("ai-memory", fetch=gh)
    assert s["out"] == str(scout_home / "ai-memory") and s["token"] is False
    assert [r["full_name"] for r in s["new"]] == ["a/big", "a/small"]
    c = ms.scout_candidates("ai-memory", status="new")
    assert c["total"] == 2 and c["repos"][0]["full_name"] == "a/big" and c["repos"][0]["interface"] == ["pip"]
    assert ms.scout_candidates("ai-memory", text="SMALL")["total"] == 1
    assert ms.scout_candidates("ai-memory", min_stars=100)["total"] == 1
    lim = ms.scout_candidates("ai-memory", limit=1)
    assert lim["truncated"] and len(lim["repos"]) == 1


def test_candidates_before_any_run():
    with pytest.raises(FileNotFoundError, match="scout_run first"):
        ms.scout_candidates("ai-memory")
    with pytest.raises(ValueError):
        ms.scout_candidates("ai-memory", status="maybe")


def test_mark_known_drops_it_from_later_runs(scout_home):
    gh = FakeGitHub([repo("a/one"), repo("b/two")])
    ms.scout_run("ai-memory", fetch=gh)
    r = ms.scout_mark_known("ai-memory", "https://github.com/a/one/", note="measured")
    assert r["added"] and ms.scout_candidates("ai-memory", status="known")["repos"][0]["full_name"] == "a/one"
    assert "a/one  # measured" in (scout_home / "ai-memory" / "known.txt").read_text(encoding="utf-8")
    assert not ms.scout_mark_known("ai-memory", "A/One")["added"]  # once only
    gh.items = [repo("a/one"), repo("c/three")]
    s = ms.scout_run("ai-memory", fetch=gh)
    assert [x["full_name"] for x in s["new"]] == ["c/three"]
    with pytest.raises(ValueError):
        ms.scout_mark_known("ai-memory", "not-a-repo")


def test_out_folder_like_the_cli(tmp_path):
    out = tmp_path / "adebench" / "scout"
    ms.scout_run("ai-memory", out=str(out), fetch=FakeGitHub([repo("a/one")]))
    assert (out / "candidates.json").is_file()
    assert ms.scout_candidates("ai-memory", out=str(out))["total"] == 1


def test_doctor_reports_quota():
    def fetch(url, accept):
        body = {"resources": {k: {"remaining": 9, "limit": 10, "reset": 1} for k in ("search", "core")}}
        return 200, {}, json.dumps(body)
    d = ms.doctor(fetch)
    assert d["search"]["remaining"] == 9 and d["token"] is False


def test_server_over_stdio(scout_home):
    mcp = pytest.importorskip("mcp")
    from mcp.client.stdio import StdioServerParameters, stdio_client

    async def talk():
        params = StdioServerParameters(command=sys.executable, args=["-m", "topicscout", "mcp"],
                                       env={**__import__("os").environ, "TOPICSCOUT_HOME": str(scout_home)})
        async with stdio_client(params) as (r, w):
            async with mcp.ClientSession(r, w) as session:
                await session.initialize()
                tools = {t.name for t in (await session.list_tools()).tools}
                res = await session.call_tool("list_profiles", {})
                return tools, json.loads(res.content[0].text)

    tools, profiles = asyncio.run(talk())
    assert tools == {"list_profiles", "scout_run", "scout_candidates", "scout_mark_known", "doctor"}
    assert "github-scrapers" in profiles["profiles"]
