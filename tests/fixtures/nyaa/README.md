# Nyaa fixtures

**These are constructed samples.** They reproduce the structure of Nyaa Sukebei
pages (`/?page=rss` and the HTML search page) as documented and observed from the
public site, but the values (titles, info hashes, uploaders, counts) are made up.

Rules for this directory:

- No fixture may be produced by "recording a successful scrape" of content the
  project is not allowed to fetch; the parser must be tested against structure,
  not against real payloads.
- Every fixture is static: tests never hit the network.
- When the real site changes, add a new fixture that mirrors the new structure,
  then update the selectors in `app/sources/nyaa.py`. Do not edit an old fixture
  to make a broken parser pass.

Files:

| File | Purpose |
|---|---|
| `rss_fc2.xml` | RSS with FC2 items, a non-FC2 item and one item without a hash |
| `rss_missing_fields.xml` | RSS item missing `nyaa:infoHash`, `pubDate` and `nyaa:size` |
| `search_page1.html` | HTML search page, two rows (one with a magnet link) |
| `search_page2.html` | HTML search page, older row used by the pagination fallback |
| `search_empty.html` | Valid page with an empty torrent table (structure change guard) |
| `error_page.html` | Cloudflare-style error page (must raise, never "succeed") |
