# Product site

Static landing page for Vinyl Streamer prebuilts and DIY entry points.

## Local preview

```bash
cd site
python3 -m http.server 4173
# open http://127.0.0.1:4173
```

## GitHub Pages

A workflow (`.github/workflows/pages.yml`) deploys this folder on pushes to `main`.

One-time repo setup (Settings → Pages):

1. **Source:** GitHub Actions
2. Merge the workflow, then run **Deploy product site** if needed
3. Site URL: `https://palavido-dev.github.io/vinyl-airplay/`

### Custom domain (later)

When you buy a domain, add a `site/CNAME` file with the hostname and point DNS at GitHub Pages. No need to buy a domain until orders justify it — Pages is enough for the waitlist phase.
