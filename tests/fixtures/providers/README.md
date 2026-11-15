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
- live verification status (see `docs/deviations.md` §19):
  - verified against real pages: `fc2.html` (og:* title/description/cover,
    protocol-relative sample images, site-chrome h1/h2),
    `fc2_article_with_description.html` + `fc2_description_widget.html`
    (structure captured through the Japan proxy on 2026-11-15: the article page
    only carries a truncated `og:description` and embeds the real text in
    `/widget/article/<number>/description?ac=…`; the widget puts it in one
    `div[data-container]` under `body`, mixed with ad blocks, inline scripts and
    hidden tracking nodes. Text is invented, structure is real), `mmtv.html`
    (`div.video` + `h3.video-title > a`), `javdb.html` (`.movie-list .item > a.box`,
    title in `.video-title > strong`), `freejavbt.html`
    (`.video-list-item > .card`, `h5.card-title` + `p.card-text`, lazy `data-src`),
    `madou_not_found.html` (200 + 404 panel);
  - still constructed: `fc2_hub.html`, `javlibrary.html`, `miss_av.html`,
    `avsox.html` (403 / JS challenge from the build environment).

| File | Provider | Shape |
|---|---|---|
| `fc2.html` | Fc2 | article detail page (og:* meta tags, protocol-relative images) |
| `fc2_article_with_description.html` | Fc2 | article page that embeds the description widget (truncated og:description + iframe) |
| `fc2_description_widget.html` | Fc2 | description widget body: `div[data-container]` + ad/script/hidden furniture |
| `fc2_hub.html` | fc2_hub | search result cards |
| `javdb.html` | Javdb | `.movie-list` grid |
| `mmtv.html` | Mmtv | `.video-item` cards |
| `freejavbt.html` | freejavbt | WordPress `article` |
| `miss_av.html` | miss_av | thumbnail grid |
| `javlibrary.html` | Javlibrary | detail page with jacket + previews |
| `avsox.html` | avsox | `#waterfall` grid |
| `madou.html` | Madou | WordPress `article` |
| `nyaa_view.html` | Nyaa fallback | torrent view page |
