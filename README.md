# מבזקונים

A personal news-flash reader inspired by the old Android app. It's a web app:
open it in Chrome on your phone and choose **Add to Home screen**.

## How it works
- `scripts/fetch_feeds.py` collects every source in `sources.json` and writes `data/feeds.json`.
  Each source tries the site's own RSS feed first. If that fails, it uses a Google News feed for that site.
- `.github/workflows/update-feeds.yml` runs the script on GitHub every ~10 minutes.
- `index.html` (served by GitHub Pages) shows the news: categories, per-site filters, search,
  saved articles, sharing, dark mode, text size, and turning sources on or off.

## Add or change a source
Edit `sources.json`. Set `url` to the site's RSS address, and/or set `fallback` to the site's domain
to use Google News. The next run picks up the change.
