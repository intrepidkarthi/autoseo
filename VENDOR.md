# Vendored code

Third-party and cross-repo code is copied in rather than depended on, so this repo has no upstream
that can churn or disappear. Each entry records where it came from and why it is here.

## `vendor/scan_marks.py`

**From:** the `write-like-me` skill, v4.2.0, `scripts/scan_marks.py`
**Licence:** same author (the skill bundles it); its Unicode tables are adapted from
[`guillaumemeyer/watermarks-remover`](https://github.com/guillaumemeyer/watermarks-remover) (MIT).

Copied because a skill is instructions for an agent at a terminal, and this pipeline publishes at
06:00 with nobody there. The plugin cache it normally lives in does not exist on a GitHub runner.

It is the only check in `quality/` that cannot be done by reading. Zero-width characters, bidi
controls, tag characters, space homoglyphs and the private-use delimiters ChatGPT wraps citation
tokens in have no glyph — a model asked whether its own output is clean will say yes, because it
looks clean. `quality/marks.py` calls `scan()` and `clean()` from this file and blocks on anything
that survives.

**Changed on copy:** nothing. A provenance banner is inserted after the shebang; the code is
byte-for-byte upstream so it can be re-copied wholesale on the next skill release.

**If the skill updates,** re-copy the file and re-add the banner. Do not patch this copy — the
Unicode tables are the part most likely to gain entries, and hand-merging them is how a codepoint
gets dropped.

## `vendor/render_articles.py`

**From:** `intrepidkarthi/dailyvox` → `render_articles.py` at the website root. That root was
`solyn/website/` until 2026-08-14, when the repo hoisted the whole website out of the iOS target's
directory ahead of the Android port; it is `website/` now. Verified byte-identical to upstream at
the new location on 2026-08-14, the three edits below excepted.
**Licence:** same project, same author (MIT).

Copied because the original is **gitignored in that repo** (`.gitignore:81`) and so exists only on
one machine. The site is served by Vercel with `buildCommand: null` and `outputDirectory: public`,
meaning nothing runs a build step — committing markdown into `content/articles/` produces no page.
PR #68 merged and was invisible for exactly this reason.

autoseo runs this to generate `public/blog/<slug>.html` and `sitemap-articles.xml`, and commits the
output alongside the markdown. Same renderer, so published posts match the seven existing articles
byte-for-byte in structure.

**Changed on copy:** `SRC`, `OUT` and the sitemap path are read from environment variables instead
of being derived from the file's own location, since it no longer sits beside the content.

**If the site's renderer changes,** re-copy the file and re-apply those three edits rather than
patching this copy by hand.
