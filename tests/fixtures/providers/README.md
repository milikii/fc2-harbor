# Provider fixtures

**These are constructed samples.** Each file mirrors the structure the adapter's
selectors expect; the titles, URLs and images are made up. No page was captured
from a live site and stored here.

Why: several of these sites are behind Cloudflare or require a Japan exit, and
storing scraped pages of that content in the repository is not something this
project does. The tests therefore prove *selector/merge behaviour*, not that the
sites still look like this.

Rules:

- a provider whose real markup differs must get a **new** fixture plus an updated
  adapter; never edit an old fixture to make a broken parser pass;
- every adapter must raise `ProviderError("structure_changed")` when its anchors
  are missing, so a silent empty result is impossible;
- live verification is manual: fetch the real page and compare against the fixture's
  structure. `fc2.html` mirrors a real article page (og:* title/description/cover,
  protocol-relative sample images, site-chrome h1/h2 that must never be used as the
  title); the other eight could not be reached from the build environment
  (Cloudflare / 403 / JS challenge) and are unverified against live markup.

| File | Provider | Shape |
|---|---|---|
| `fc2.html` | Fc2 | article detail page (og:* meta tags, protocol-relative images) |
| `fc2_hub.html` | fc2_hub | search result cards |
| `javdb.html` | Javdb | `.movie-list` grid |
| `mmtv.html` | Mmtv | `.video-item` cards |
| `freejavbt.html` | freejavbt | WordPress `article` |
| `miss_av.html` | miss_av | thumbnail grid |
| `javlibrary.html` | Javlibrary | detail page with jacket + previews |
| `avsox.html` | avsox | `#waterfall` grid |
| `madou.html` | Madou | WordPress `article` |
| `nyaa_view.html` | Nyaa fallback | torrent view page |
