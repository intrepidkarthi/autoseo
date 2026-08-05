# Vendored code

Third-party and cross-repo code is copied in rather than depended on, so this repo has no upstream
that can churn or disappear. Each entry records where it came from and why it is here.

## `vendor/render_articles.py`

**From:** `intrepidkarthi/dailyvox` → `solyn/website/render_articles.py`
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
