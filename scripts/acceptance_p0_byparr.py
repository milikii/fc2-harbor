#!/usr/bin/env python3
"""P0 acceptance for the two Byparr pilot providers, three layers per code.

Layer 1 (link): Byparr returns the business HTML — target 200, not a challenge,
    a title node is present; wall time and Byparr latency are recorded.
Layer 2 (fields): the *real* adapter parses that HTML and yields a censorship
    tag, the original Japanese title and a non-empty description.
Layer 3 (card): the *real* card builder renders it — the tag is not 未知 and
    both title and description show a Chinese translation.

The script is read-only: it never opens the database and never writes a row.
It is meant to run inside the NAS ``bt-automation`` container (which has the
project dependencies), with this branch's code on ``PYTHONPATH``::

    docker exec -e PYTHONPATH=/tmp/p0 bt-automation \
        python /tmp/p0/scripts/acceptance_p0_byparr.py \
        --byparr-url http://192.168.1.220:8191 \
        --proxy-url http://192.168.1.220:7890 \
        --out /tmp/p0-artifacts

Codes (all taken from repository records, never invented):

* ``4559944`` — the one number the records prove is delisted on official FC2
  (``SOURCES-ADVICE.md``: お探しの商品が見つかりませんでした). Used as the
  negative control on both sites.
* ``3341582`` / ``2859042`` — real cards recorded on the miss_av result page for
  4559944 (``SOURCES-ADVICE.md``).
* ``4761220`` — real FC2 number in the repo's Nyaa/legacy records
  (``app/sources/nyaa.py``, ``tests/fixtures/nyaa/search_page_realistic.html``).
* ``4910695`` — real FC2 number in ``docs/deviations.md`` §52.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.db.enums import Censorship, ContentType
from app.db.models import Content
from app.network.challenge import classify_page
from app.network.clients import HttpClient
from app.network.flaresolverr import FlareSolverrClient, FlareSolverrError
from app.providers.base import ProviderError, ProviderPage, infer_censorship
from app.providers.fc2_hub import Fc2HubProvider
from app.providers.miss_av import MissAvProvider
from app.services.cards import (
    CardSource,
    build_card_messages,
    card_text,
    censorship_label,
    clean_card_title,
)
from app.services.translate import (
    GoogleFreeTranslator,
    MyMemoryTranslator,
    TranslationError,
    looks_chinese,
)
from selectolax.lexbor import LexborHTMLParser  # type: ignore[import-not-found]

#: provider id → (adapter class, codes). Provenance in the module docstring.
CASES: dict[str, tuple[type, list[str]]] = {
    "miss_av": (MissAvProvider, ["4559944", "3341582", "2859042"]),
    "fc2_hub": (Fc2HubProvider, ["4559944", "4761220", "4910695"]),
}

DELISTED = "4559944"


def _content(code: str) -> Content:
    return Content(
        content_type=ContentType.FC2,
        canonical_key=f"fc2_ppv:{code}",
        display_code=f"FC2-PPV-{code}",
    )


def _anchor_stats(html: str, provider_id: str) -> dict[str, Any]:
    tree = LexborHTMLParser(html)
    stats: dict[str, Any] = {
        "title_tag": tree.css_first("title") is not None,
        "h1": tree.css_first("h1") is not None,
        "og_title": tree.css_first('meta[property="og:title"]') is not None,
    }
    if provider_id == "miss_av":
        cards = tree.css(".thumbnail, .grid .item, .item")
        stats["cards"] = len(cards)
        stats["card_title_nodes"] = sum(
            1 for card in cards if card.css_first(".text-secondary, .title, a") is not None
        )
    else:
        stats["canonical"] = tree.css_first('link[rel="canonical"]') is not None
    return stats


def _resolve_censorship(parsed: Any) -> tuple[Censorship, str, list[str]]:
    """Mirror ``MetadataCoordinator._apply_censorship_title_fallback`` for FC2.

    Both pilot adapters return no explicit tag, so the real coordinator infers it
    from the title and falls back to the FC2 default (CENSORED) when the title has
    no evidence. Doing the same here keeps layer 2 faithful without a DB.
    """

    if parsed is not None and parsed.censorship is not None:
        return parsed.censorship, "provider", []
    inferred, evidence = infer_censorship(
        {"title": parsed.title_ja if parsed else None, "release_title": None}
    )
    if inferred is not None:
        return inferred, "title_fallback", list(evidence)
    # The coordinator's FC2 default: an FC2 product is censored unless proven otherwise.
    return Censorship.CENSORED, "default", list(evidence)


async def _probe_translation(description: str, proxy_url: str) -> dict[str, Any]:
    """Check whether the translation *backend* works, independent of the gate."""

    attempts: list[dict[str, Any]] = []
    for name, factory in (
        ("google_free", GoogleFreeTranslator),
        ("mymemory", MyMemoryTranslator),
    ):
        translator = None
        started = time.monotonic()
        try:
            translator = factory(proxy_url=proxy_url)
            translated = await translator.translate(description)
            error = None
        except TranslationError as exc:
            translated, error = None, {"code": exc.code, "message": str(exc)}
        except Exception as exc:
            translated, error = None, {"code": type(exc).__name__, "message": str(exc)}
        finally:
            if translator is not None:
                await translator.aclose()
        attempts.append(
            {
                "provider": name,
                "elapsed_ms": int((time.monotonic() - started) * 1000),
                "translated_chars": len(translated or ""),
                "translated_preview": (translated or "")[:120],
                "looks_chinese": bool(translated and looks_chinese(translated)),
                "error": error,
            }
        )
        if translated and looks_chinese(translated):
            break
    return {
        "ok": any(item["looks_chinese"] for item in attempts),
        "attempts": attempts,
    }


async def _run_case(
    provider_id: str,
    adapter_cls: type,
    code: str,
    *,
    solver: FlareSolverrClient,
    proxy_url: str,
    out_dir: Path,
    delay_seconds: float,
) -> dict[str, Any]:
    content = _content(code)
    provider = adapter_cls(client=HttpClient())
    display_code = content.display_code or f"FC2-PPV-{code}"
    url = provider.build_url(content)
    case: dict[str, Any] = {
        "provider": provider_id,
        "code": code,
        "delisted_on_official_fc2": code == DELISTED,
        "url": url,
        "started_at": datetime.now(UTC).isoformat(),
    }

    # ---- layer 1: link -----------------------------------------------------
    started = time.monotonic()
    result = None
    error = None
    try:
        result = await solver.request_get(
            url,
            proxy_url=proxy_url,
            required_selectors=provider.REQUIRED_SELECTORS,
        )
    except FlareSolverrError as exc:
        error = {"code": exc.code, "message": str(exc)}
    wall_ms = int((time.monotonic() - started) * 1000)

    link: dict[str, Any] = {
        "wall_ms": wall_ms,
        "byparr_error": error,
        "target_status": None,
        "html_bytes": 0,
        "byparr_latency_ms": None,
        "attempts": None,
        "has_cf_clearance": None,
        "verdict": None,
        "verdict_reason": None,
        "anchors": {},
        "pass": False,
    }
    if result is not None:
        verdict = classify_page(
            status=result.status,
            headers=None,
            html=result.html,
            required_selectors=provider.REQUIRED_SELECTORS,
        )
        anchors = _anchor_stats(result.html, provider_id)
        title_present = anchors["h1"] or anchors["og_title"]
        anchor_present = (
            anchors.get("cards", 0) > 0
            if provider_id == "miss_av"
            else anchors.get("canonical", False) and anchors["og_title"]
        )
        link.update(
            {
                "target_status": result.status,
                "html_bytes": len(result.html.encode("utf-8")),
                "byparr_latency_ms": result.latency_ms,
                "attempts": result.attempts,
                "has_cf_clearance": result.has_cf_clearance,
                "verdict": verdict.kind,
                "verdict_reason": verdict.reason,
                "anchors": anchors,
                "pass": (
                    result.status == 200
                    and verdict.kind == "ok"
                    and title_present
                    and anchor_present
                ),
            }
        )
        (out_dir / f"{provider_id}-{code}.html").write_text(result.html, encoding="utf-8")
    case["layer1_link"] = link

    # ---- layer 2: fields ---------------------------------------------------
    fields: dict[str, Any] = {
        "parse_error": None,
        "title_ja": None,
        "description_ja": None,
        "censorship": None,
        "censorship_source": None,
        "censorship_evidence": [],
        "pass": False,
    }
    parsed = None
    if result is not None:
        page = ProviderPage(url=result.url or url, html=result.html, status=result.status)
        parse_started = time.monotonic()
        try:
            parsed = provider.parse(page, content)
        except ProviderError as exc:
            fields["parse_error"] = {"code": exc.code, "message": str(exc)}
        except Exception as exc:
            fields["parse_error"] = {"code": type(exc).__name__, "message": str(exc)}
        fields["parse_ms"] = int((time.monotonic() - parse_started) * 1000)
        if parsed is None and fields["parse_error"] is None:
            fields["parse_error"] = {"code": "no_match", "message": "adapter returned None"}
        if parsed is not None:
            censorship, source, evidence = _resolve_censorship(parsed)
            fields.update(
                {
                    "title_ja": parsed.title_ja,
                    "description_ja": parsed.description_ja,
                    "official_url": parsed.official_url,
                    "description_quality": parsed.description_quality,
                    "censorship": censorship.value,
                    "censorship_source": source,
                    "censorship_evidence": evidence,
                    "pass": bool(
                        parsed.title_ja
                        and parsed.description_ja
                        and censorship is not Censorship.UNKNOWN
                    ),
                }
            )
    case["layer2_fields"] = fields

    # ---- layer 3: card -----------------------------------------------------
    card: dict[str, Any] = {
        "label": None,
        "label_pass": False,
        "title_rendered": None,
        "title_translated": False,
        "body_rendered": None,
        "body_translated": False,
        "card_pass": False,
        "caption_preview": None,
        "pipeline_would_translate": provider_id == "Fc2",
        "pipeline_gate_reason": (
            "enrich() only translates when description_ja came from Fc2; "
            f"this provider is {provider_id!r}"
        ),
    }
    translation_probe: dict[str, Any] | None = None
    simulated: dict[str, Any] | None = None
    if parsed is not None:
        censorship, source, _ = _resolve_censorship(parsed)
        source_card = CardSource(
            content_id="acceptance",
            canonical_key=content.canonical_key,
            display_code=display_code,
            title=parsed.title_ja,
            description_ja=parsed.description_ja,
            description_zh=None,
            censorship=censorship,
            nyaa_url=f"https://sukebei.nyaa.si/view/{code}",
            official_url=parsed.official_url,
        )
        label = censorship_label(censorship)
        title_text = clean_card_title(source_card)
        body_text = card_text(source_card)
        messages = build_card_messages(source_card)
        card.update(
            {
                "label": label,
                "label_pass": label != censorship_label(Censorship.UNKNOWN),
                "title_rendered": title_text,
                "title_translated": looks_chinese(title_text),
                "body_rendered": body_text,
                "body_translated": bool(body_text and looks_chinese(body_text)),
                "caption_preview": messages.caption[:300],
            }
        )
        card["card_pass"] = (
            card["label_pass"] and card["title_translated"] and card["body_translated"]
        )

        if parsed.description_ja:
            translation_probe = await _probe_translation(parsed.description_ja, proxy_url)
            translated = next(
                (
                    item["translated_preview"]
                    for item in translation_probe["attempts"]
                    if item["looks_chinese"]
                ),
                None,
            )
            if translated:
                # What the card *would* show if enrich() had stored description_zh.
                simulated_card = CardSource(
                    content_id="acceptance",
                    canonical_key=content.canonical_key,
                    display_code=display_code,
                    title=parsed.title_ja,
                    description_ja=parsed.description_ja,
                    description_zh=translated,
                    censorship=censorship,
                    nyaa_url=f"https://sukebei.nyaa.si/view/{code}",
                    official_url=parsed.official_url,
                )
                simulated = {
                    "body_translated": looks_chinese(card_text(simulated_card) or ""),
                    "title_translated": looks_chinese(clean_card_title(simulated_card)),
                }
    case["layer3_card"] = card
    case["translation_backend"] = translation_probe
    case["simulated_with_description_zh"] = simulated

    await asyncio.sleep(delay_seconds)
    return case


def _summary(cases: list[dict[str, Any]]) -> str:
    lines = [
        "provider  code      l1_link  l2_fields  l3_card  wall_s  byparr_s  verdict  label",
        "--------  --------  -------  ---------  -------  ------  --------  -------  -----",
    ]
    for case in cases:
        link = case["layer1_link"]
        fields = case["layer2_fields"]
        card = case["layer3_card"]
        wall = link["wall_ms"] / 1000
        byparr = (link["byparr_latency_ms"] or 0) / 1000
        verdict = link["verdict"] or (link["byparr_error"] or {}).get("code", "-")
        lines.append(
            f"{case['provider']:<8}  {case['code']:<8}  "
            f"{'PASS' if link['pass'] else 'FAIL':<7}  "
            f"{'PASS' if fields['pass'] else 'FAIL':<9}  "
            f"{'PASS' if card['card_pass'] else 'FAIL':<7}  "
            f"{wall:>6.1f}  {byparr:>8.1f}  {verdict:<7}  {card['label'] or '-'}"
        )
    return "\n".join(lines)


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--byparr-url", default="http://192.168.1.220:8191")
    parser.add_argument("--proxy-url", default="http://192.168.1.220:7890")
    parser.add_argument("--out", type=Path, default=Path(tempfile.gettempdir()) / "p0-artifacts")
    parser.add_argument("--providers", nargs="*", default=list(CASES))
    parser.add_argument("--delay", type=float, default=3.0)
    args = parser.parse_args(argv)

    args.out.mkdir(parents=True, exist_ok=True)
    solver = FlareSolverrClient(
        args.byparr_url,
        timeout_seconds=95.0,
        max_timeout_ms=60_000,
        attempts=2,
    )
    cases: list[dict[str, Any]] = []
    try:
        for provider_id in args.providers:
            adapter_cls, codes = CASES[provider_id]
            for code in codes:
                print(f"[acceptance] {provider_id} {code} …", flush=True)
                case = await _run_case(
                    provider_id,
                    adapter_cls,
                    code,
                    solver=solver,
                    proxy_url=args.proxy_url,
                    out_dir=args.out,
                    delay_seconds=args.delay,
                )
                cases.append(case)
                print(
                    f"[acceptance] {provider_id} {code} "
                    f"l1={case['layer1_link']['pass']} "
                    f"l2={case['layer2_fields']['pass']} "
                    f"l3={case['layer3_card']['card_pass']}",
                    flush=True,
                )
    finally:
        await solver.aclose()

    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "byparr_url": args.byparr_url,
        "proxy_url": args.proxy_url,
        "cases": cases,
        "byparr_stats": solver.stats,
        "byparr_circuit": solver.circuit_state(),
    }
    (args.out / "result.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    summary = _summary(cases)
    (args.out / "summary.txt").write_text(summary + "\n", encoding="utf-8")
    print("\n" + summary, flush=True)
    print(f"\n[acceptance] artifacts: {args.out}", flush=True)
    return 0 if all(case["layer1_link"]["pass"] for case in cases) else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
