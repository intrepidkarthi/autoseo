# autoseo

SEO/AEO measurement and gated content automation for [DailyVox](https://getdailyvox.com).

Runs entirely on GitHub Actions. No server, no database to host, ~$0/month.

- **[DESIGN.md](DESIGN.md)** — architecture, module map, decision engine, quality gate
- **[SETUP.md](SETUP.md)** — every credential, with click-by-click steps

## Status

**Phase 0 — measurement.** Reads only; publishes nothing.

| Phase | | |
|---|---|---|
| 0 | measurement — GSC, URL Inspection, Bing | ✅ built |
| 1 | finish the de-listing of orphaned pages | next |
| 2 | quality gate — AI-slop + plagiarism | |
| 3 | Telegram approval gate | |
| 4 | blog publishing (PR to the site repo) | |
| 5 | social publishing + scheduler | |
| 6 | decision engine (bandit) | |
| 7 | video | |
| 8 | AEO prompt panel | |

## Usage

```bash
pip install -e .
cp .env.example .env          # see SETUP.md for how to fill it

autoseo inventory             # build the URL list from the sitemap
autoseo collect               # what CI runs: gsc + bing + inspect + report
autoseo report                # per-cluster indexation ratio
```

`autoseo inventory --public-dir /path/to/site/public` also picks up URLs that exist on disk but are
absent from the sitemap. That gap is the point: getdailyvox.com serves 1,724 pages while the sitemap
lists 156.

## Design notes

**Statistics decide allocation, the LLM decides craft.** Scheduling is a multi-armed bandit — free,
deterministic, auditable. The model writes copy and judges freshness, nothing else.

**A module that reads the open web never holds publishing credentials.** Enforced by binding each
workflow to a different GitHub Environment, not by convention. A prompt injection reaching the
research step has no path to a posting token.

**Nothing publishes without human approval**, per channel, until a channel earns autonomy.

## License

MIT. Third-party code is vendored rather than depended on — see `VENDOR.md` when it appears.
