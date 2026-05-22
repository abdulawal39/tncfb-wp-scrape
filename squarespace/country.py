"""Best-effort country detection for a domain.

Order of confidence:
  1. ccTLD on the domain (authoritative when present) — e.g. .co.uk -> GB
  2. og:locale region in homepage HTML  — e.g. en_GB -> GB
  3. <html lang="..-XX"> region          — e.g. de-DE -> DE

Generic TLDs (.com/.org/.net/...) carry no country, so for those we rely
on HTML signals; if none are present the result is "" (unknown).
Output is an ISO-3166-1 alpha-2 code (uppercase), or "" if unknown.
"""

from __future__ import annotations

import re

# ccTLD -> ISO-2. Most ccTLDs already equal the ISO code; the exceptions
# (.uk->GB, etc.) and common second-level suffixes are listed explicitly.
CCTLD_MAP = {
    "ac": "AS", "ad": "AD", "ae": "AE", "af": "AF", "ag": "AG", "ai": "AI",
    "al": "AL", "am": "AM", "ao": "AO", "ar": "AR", "at": "AT", "au": "AU",
    "az": "AZ", "ba": "BA", "bd": "BD", "be": "BE", "bf": "BF", "bg": "BG",
    "bh": "BH", "bi": "BI", "bj": "BJ", "bn": "BN", "bo": "BO", "br": "BR",
    "bs": "BS", "bt": "BT", "bw": "BW", "by": "BY", "bz": "BZ", "ca": "CA",
    "cd": "CD", "cf": "CF", "cg": "CG", "ch": "CH", "ci": "CI", "cl": "CL",
    "cm": "CM", "cn": "CN", "co": "CO", "cr": "CR", "cu": "CU", "cv": "CV",
    "cy": "CY", "cz": "CZ", "de": "DE", "dj": "DJ", "dk": "DK", "dm": "DM",
    "do": "DO", "dz": "DZ", "ec": "EC", "ee": "EE", "eg": "EG", "es": "ES",
    "et": "ET", "eu": "EU", "fi": "FI", "fj": "FJ", "fm": "FM", "fo": "FO",
    "fr": "FR", "ga": "GA", "gd": "GD", "ge": "GE", "gg": "GG", "gh": "GH",
    "gi": "GI", "gl": "GL", "gm": "GM", "gp": "GP", "gr": "GR", "gt": "GT",
    "gu": "GU", "gy": "GY", "hk": "HK", "hn": "HN", "hr": "HR", "ht": "HT",
    "hu": "HU", "id": "ID", "ie": "IE", "il": "IL", "im": "IM", "in": "IN",
    "iq": "IQ", "ir": "IR", "is": "IS", "it": "IT", "je": "JE", "jm": "JM",
    "jo": "JO", "jp": "JP", "ke": "KE", "kg": "KG", "kh": "KH", "ki": "KI",
    "km": "KM", "kn": "KN", "kr": "KR", "kw": "KW", "ky": "KY", "kz": "KZ",
    "la": "LA", "lb": "LB", "lc": "LC", "li": "LI", "lk": "LK", "lr": "LR",
    "ls": "LS", "lt": "LT", "lu": "LU", "lv": "LV", "ly": "LY", "ma": "MA",
    "mc": "MC", "md": "MD", "me": "ME", "mg": "MG", "mk": "MK", "ml": "ML",
    "mm": "MM", "mn": "MN", "mo": "MO", "mq": "MQ", "mr": "MR", "mt": "MT",
    "mu": "MU", "mv": "MV", "mw": "MW", "mx": "MX", "my": "MY", "mz": "MZ",
    "na": "NA", "nc": "NC", "ne": "NE", "ng": "NG", "ni": "NI", "nl": "NL",
    "no": "NO", "np": "NP", "nr": "NR", "nz": "NZ", "om": "OM", "pa": "PA",
    "pe": "PE", "pf": "PF", "pg": "PG", "ph": "PH", "pk": "PK", "pl": "PL",
    "pr": "PR", "ps": "PS", "pt": "PT", "py": "PY", "qa": "QA", "re": "RE",
    "ro": "RO", "rs": "RS", "ru": "RU", "rw": "RW", "sa": "SA", "sb": "SB",
    "sc": "SC", "sd": "SD", "se": "SE", "sg": "SG", "si": "SI", "sk": "SK",
    "sl": "SL", "sm": "SM", "sn": "SN", "so": "SO", "sr": "SR", "st": "ST",
    "sv": "SV", "sy": "SY", "sz": "SZ", "tc": "TC", "td": "TD", "tg": "TG",
    "th": "TH", "tj": "TJ", "tn": "TN", "to": "TO", "tr": "TR", "tt": "TT",
    "tw": "TW", "tz": "TZ", "ua": "UA", "ug": "UG", "uk": "GB", "us": "US",
    "uy": "UY", "uz": "UZ", "vc": "VC", "ve": "VE", "vg": "VG", "vi": "VI",
    "vn": "VN", "vu": "VU", "ws": "WS", "ye": "YE", "za": "ZA", "zm": "ZM",
    "zw": "ZW",
}

RE_OG_LOCALE = re.compile(
    r'property=["\']og:locale["\'][^>]*content=["\'][a-z]{2}[_-]([A-Za-z]{2})["\']',
    re.I,
)
RE_HTML_LANG = re.compile(
    r'<html[^>]*\blang=["\'][a-z]{2}-([A-Za-z]{2})["\']',
    re.I,
)
# schema.org PostalAddress.addressCountry — the business's own stated country.
# Matches JSON-LD ( "addressCountry": "DE" ) and microdata
# ( itemprop="addressCountry">United Kingdom< ) forms.
RE_ADDR_COUNTRY = re.compile(
    r'addressCountry["\']?[^>:=]{0,20}?[:=>]\s*["\']?\s*'
    r'([A-Za-z][A-Za-z .\']{1,40}?)\s*["\'<,}]',
    re.I,
)
RE_TEL = re.compile(r'tel:\s*\+(\d[\d\s().-]{5,})', re.I)
RE_GBP = re.compile(r'£\s*\d')

# Map both ISO-2 and common full country names found inside an addressCountry.
COUNTRY_NAMES = {
    "united kingdom": "GB", "great britain": "GB", "england": "GB",
    "scotland": "GB", "wales": "GB", "uk": "GB",
    "united states": "US", "usa": "US", "u.s.a.": "US", "america": "US",
    "germany": "DE", "deutschland": "DE", "france": "FR", "italy": "IT",
    "italia": "IT", "spain": "ES", "espana": "ES", "españa": "ES",
    "netherlands": "NL", "nederland": "NL", "belgium": "BE", "belgie": "BE",
    "switzerland": "CH", "austria": "AT", "österreich": "AT", "sweden": "SE",
    "norway": "NO", "denmark": "DK", "finland": "FI", "portugal": "PT",
    "ireland": "IE", "poland": "PL", "polska": "PL", "greece": "GR",
    "czech republic": "CZ", "czechia": "CZ", "romania": "RO", "hungary": "HU",
    "croatia": "HR", "russia": "RU", "ukraine": "UA", "turkey": "TR",
    "türkiye": "TR", "israel": "IL", "united arab emirates": "AE",
    "saudi arabia": "SA", "egypt": "EG", "south africa": "ZA", "nigeria": "NG",
    "kenya": "KE", "morocco": "MA", "india": "IN", "pakistan": "PK",
    "bangladesh": "BD", "sri lanka": "LK", "china": "CN", "japan": "JP",
    "south korea": "KR", "korea": "KR", "vietnam": "VN", "viet nam": "VN",
    "thailand": "TH", "malaysia": "MY", "singapore": "SG", "indonesia": "ID",
    "philippines": "PH", "hong kong": "HK", "taiwan": "TW", "australia": "AU",
    "new zealand": "NZ", "brazil": "BR", "brasil": "BR", "argentina": "AR",
    "chile": "CL", "colombia": "CO", "peru": "PE", "mexico": "MX",
    "méxico": "MX", "canada": "CA", "uruguay": "UY", "ecuador": "EC",
    "venezuela": "VE",
}

# International calling code -> ISO-2. +1 (US/CA) is intentionally omitted
# because it is ambiguous. Matched longest-prefix-first.
CALLING_CODES = {
    "44": "GB", "49": "DE", "33": "FR", "39": "IT", "34": "ES", "31": "NL",
    "32": "BE", "41": "CH", "43": "AT", "46": "SE", "47": "NO", "45": "DK",
    "358": "FI", "351": "PT", "353": "IE", "48": "PL", "30": "GR", "36": "HU",
    "420": "CZ", "421": "SK", "40": "RO", "359": "BG", "385": "HR", "386": "SI",
    "372": "EE", "371": "LV", "370": "LT", "7": "RU", "380": "UA", "90": "TR",
    "972": "IL", "971": "AE", "966": "SA", "20": "EG", "27": "ZA", "234": "NG",
    "254": "KE", "212": "MA", "91": "IN", "92": "PK", "880": "BD", "94": "LK",
    "86": "CN", "81": "JP", "82": "KR", "84": "VN", "66": "TH", "60": "MY",
    "65": "SG", "62": "ID", "63": "PH", "852": "HK", "886": "TW", "61": "AU",
    "64": "NZ", "55": "BR", "54": "AR", "56": "CL", "57": "CO", "51": "PE",
    "52": "MX", "598": "UY", "593": "EC", "58": "VE",
}
_CALLING_PREFIXES = sorted(CALLING_CODES, key=len, reverse=True)


def _country_from_addr(html: str) -> str:
    m = RE_ADDR_COUNTRY.search(html)
    if not m:
        return ""
    val = m.group(1).strip().lower()
    if len(val) == 2 and val.upper() in set(CCTLD_MAP.values()) | {"US"}:
        return val.upper()
    return COUNTRY_NAMES.get(val, "")


def _country_from_tel(html: str) -> str:
    for m in RE_TEL.finditer(html):
        digits = re.sub(r"\D", "", m.group(1))
        if not digits:
            continue
        for pref in _CALLING_PREFIXES:
            if digits.startswith(pref):
                return CALLING_CODES[pref]
    return ""


def country_from_domain(domain: str) -> str:
    """Return ISO-2 from a ccTLD, or '' for generic/unknown TLDs."""
    if not domain:
        return ""
    parts = domain.lower().strip().rstrip(".").split(".")
    if len(parts) < 2:
        return ""
    tld = parts[-1]
    # Two-level suffix like co.uk / com.au / com.br — the country is the TLD.
    if len(parts) >= 3 and parts[-2] in {
        "co", "com", "org", "net", "gov", "edu", "ac", "or", "ne", "go",
    }:
        return CCTLD_MAP.get(tld, "")
    return CCTLD_MAP.get(tld, "")


def country_from_html(html: str) -> str:
    """Return ISO-2 from page content signals, strongest first, or ''.

    Order: explicit address country > og:locale > <html lang> > tel: code
    > £ currency. The address and og:locale signals reflect the business's
    own locale; tel/currency are weaker tiebreaks.
    """
    if not html:
        return ""
    c = _country_from_addr(html)
    if c:
        return c
    m = RE_OG_LOCALE.search(html)
    if m:
        return m.group(1).upper()
    m = RE_HTML_LANG.search(html)
    if m:
        return m.group(1).upper()
    c = _country_from_tel(html)
    if c:
        return c
    if RE_GBP.search(html):
        return "GB"
    return ""


def detect_country(domain: str, *htmls: str | None) -> str:
    """ccTLD first (authoritative), then content signals from each HTML
    blob in order (e.g. homepage, then contact page)."""
    c = country_from_domain(domain)
    if c:
        return c
    for html in htmls:
        if html:
            c = country_from_html(html)
            if c:
                return c
    return ""
