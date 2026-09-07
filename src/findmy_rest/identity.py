"""Stable, safe identifiers for devices.

Neither field Apple gives us can be used as an MQTT topic segment, a filename or
a metric label:

* `identifier` contains `#` (an MQTT multi-level wildcard), `/`, and characters
  like `§` and `¶` -- e.g. `a:/47523436-...~#¶§§...`
* `name` is freeform: typographic apostrophes, non-breaking spaces, emoji

So every device gets an explicit `owner` and `device` slug, and `<owner>/<device>`
is the stable handle consumers key on -- an MQTT topic segment pair, a filename,
a label value.

Slugs are resolved in this order:

1. **The registry** (a ConfigMap-mounted JSON file). The override, and the only
   mechanism available for iCloud devices, which arrive with no file of their own.
2. **The key filename**, `<owner>.<device>.json`. The operator already chooses
   this when putting the key into SSM, so the parameter name and the MQTT topic
   agree by construction and there is nothing else to maintain.
3. **Slugified name**, under the configured default owner. A fallback that keeps
   a newly-paired accessory working rather than dropping it -- visible as an
   owner of `default_owner` in the output.
"""

from __future__ import annotations

import logging
import re
import unicodedata

logger = logging.getLogger(__name__)

# Deliberately strict: lowercase alphanumerics and single dashes. Safe in an MQTT
# topic (no `+`, `#`, `/`), a filename, a URL path segment and a metric label.
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
UNKNOWN = "unknown"


def slugify(text: str | None) -> str:
    """Fold arbitrary text down to a safe slug.

    Emoji, accents and typographic punctuation are stripped rather than
    transliterated: "Ty's Apple\xa0Watch" becomes "ty-s-apple-watch".
    """
    if not text:
        return UNKNOWN
    # Apostrophes are deleted rather than treated as separators, and every variant
    # identically: otherwise "John's" and "John’s" -- the same name typed two
    # ways, and Apple uses the curly form -- would produce different topics.
    text = re.sub(r"['’ʼʻ‘`]", "", text)
    # NFKD splits accents off their base letters, and the ASCII round trip drops
    # them along with emoji and anything else outside ASCII.
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", folded.lower()).strip("-")
    return slug or UNKNOWN


def valid_slug(value: str) -> bool:
    return bool(SLUG_RE.match(value))


def clean_slug(value: str, *, context: str) -> str:
    """Accept a configured slug, or complain and fall back to a safe version.

    A bad entry in the registry should be loud but must not take the service
    down: an unparseable owner is not worth losing every device's location over.
    """
    if valid_slug(value):
        return value
    fixed = slugify(value)
    logger.warning("invalid slug %r in %s; using %r instead", value, context, fixed)
    return fixed


def from_filename(stem: str) -> tuple[str, str] | None:
    """Parse the `<owner>.<device>` filename convention.

    Returns None when the stem does not carry both halves, so the caller can fall
    back rather than inventing an owner.
    """
    if "." not in stem:
        return None
    owner, _, device = stem.partition(".")
    if not owner or not device:
        return None
    return (
        clean_slug(owner, context=f"filename {stem!r}"),
        clean_slug(device, context=f"filename {stem!r}"),
    )


def parse_registry_value(value: str, *, context: str) -> tuple[str, str] | None:
    """Parse an `<owner>/<device>` registry entry."""
    owner, sep, device = value.partition("/")
    if not sep or not owner or not device:
        logger.warning(
            "registry entry %r for %s is not '<owner>/<device>'; ignoring", value, context
        )
        return None
    return clean_slug(owner, context=context), clean_slug(device, context=context)
