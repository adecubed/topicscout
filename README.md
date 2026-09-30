# topicscout

Find the GitHub repositories on a topic, and next time only the new ones.

```bash
pip install topicscout
topicscout run github-scrapers
```

```
19 found, 17 kept, 17 new -> scout-github-scrapers/new.md
```

`new.md` is a table of what appeared since the last run, most stars first: stars,
last push, language, license, a guess at the interface from the README (MCP, pip,
npm, REST, Docker...) and the description. `candidates.json` keeps everything seen,
so a weekly run is a short list, not the same 300 repos again.

No dependencies, Python 3.11+. `GITHUB_TOKEN` is optional: without it the searches
are paced to stay under GitHub's 10 per minute and READMEs are capped at 40 per run.

## Why

GitHub search is good at "repos with this topic" and bad at "what's new since I last
looked, minus the forks, the awesome-lists, the abandoned ones and the repos that
only tagged themselves". topicscout runs a fixed set of searches, applies your
filters, remembers what it has seen and what you already have.

It was the discovery step of [adebench](https://github.com/adecubed/adebench), a
benchmark for AI memory systems, which uses it to find new memories to measure.

## Commands

```bash
topicscout run PROFILE [--out DIR] [--min-stars N] [--days N] [--readme-max N]
                       [--known-from GLOB ...] [--json] [-q]
topicscout profiles    # built-in profiles
topicscout doctor      # token and remaining GitHub quota
```

- `PROFILE` is a built-in name or a path to your own JSON profile.
- `--out` is the state folder (default `scout-<profile>`): `candidates.json`,
  `new.md`, and your `known.txt`.
- `--json` prints the summary and the new repos as JSON on stdout; progress and
  errors always go to stderr, so scripts and agents can read stdout as is.

## What counts as known

Repos you already have are marked `known` and never listed as new:

- `known.txt` in the state folder: one `owner/repo` per line, `#` comments. Put
  rejected repos here too.
- `--known-from "adapters/*.py"`: the GitHub URLs in the first 20 lines of those
  files. adebench points it at its adapters, so a memory with an adapter drops out.

## Profiles

Built in: `ai-memory` (memory systems for AI agents and LLMs) and `github-scrapers`
(tools that scrape, crawl or mine GitHub). A profile is a JSON file:

```json
{
  "name": "ai-memory",
  "description": "Memory systems for AI agents and LLMs",
  "queries": ["topic:agent-memory", "\"llm memory\" in:name,description"],
  "exclude": "(?i)awesome|curated list|\\bpapers\\b",
  "require": [
    {"pattern": "(?i)memor|\\bmem\\b", "own_words": true},
    {"pattern": "(?i)\\b(agents?|llms?|ai|mcp)\\b"}
  ],
  "interfaces": [["mcp", "(?i)\\bmcp\\b"], ["pip", "(?i)\\bpip install\\b"]],
  "min_stars": 10,
  "days": 90
}
```

- `queries`: [GitHub repository search](https://docs.github.com/en/search-github/searching-on-github/searching-for-repositories)
  queries. Each one always gets `fork:false archived:false stars:>=min_stars
  pushed:>=today-days`; up to 300 results per query.
- `exclude`: a regex on name, description and topics; a match drops the repo.
- `require`: every pattern must match. With `own_words` it must be in the name or
  description, not only in a topic: a database that tagged itself `agent-memory`
  is not a memory. Hyphens and underscores count as spaces.
- `interfaces`: `[label, regex]` pairs tried on the README of each new repo.
  Leave it out and no README is fetched.
- `require_language` (default true) drops repos with no language, which are
  usually lists and docs.

Patterns are Python regexes; add `(?i)` for case-insensitive.

## Rate limits

On a 403 or 429 it waits for the reset if that is under 70 seconds; otherwise it
stops, saves what it found and says so in `new.md` and on stderr. The next run
picks up the READMEs it did not get to.

## How it compares

- [ghcrawl](https://github.com/pwrdrvr/ghcrawl) goes deep into one repository
  (issues and PRs, embeddings, clusters); topicscout goes wide across GitHub for a
  topic.
- [top-github-scraper](https://github.com/khuyentran1401/top-github-scraper)
  lists the top repos for a keyword, once; topicscout adds filters, profiles and
  memory across runs.

## License

MIT
