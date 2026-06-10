"""Deterministic synthetic data for the self-validating benchmark.

Everything here is a pure function of one :class:`random.Random` instance —
the global ``random`` module is NEVER touched, so the same seed always
produces byte-identical corpora and answers (the bench numbers published in
the README must be reproducible by anyone).

Three layers:

1. :func:`generate_corpus` — an in-memory :class:`CorpusModel`: products
   (two-word CamelCase name from fixed word lists + model number, SKU,
   valid-checksum EAN-13, 2-decimal EUR price, warranty months, power spec,
   one quoted safety sentence) plus a support URL and policy numbers.
2. :func:`write_corpus_files` — renders the model as a trusted-corpus
   folder (catalog.md / products.csv / policy.md), same layout family as
   the ``sample_corpus_dir`` test fixture.
3. :func:`generate_answers` — clean answers composed ONLY of grounded facts
   via fixed sentence templates, and corrupted answers carrying EXACTLY ONE
   injected lie each, the corruption types cycled deterministically. Every
   injected value is asserted, at generation time, to differ from every
   grounded value in the model — the bench never grades an accidental
   truth as a lie.

Import-safe: no side effects, no I/O at import time, no global randomness.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from pathlib import Path

from verigate.canonical import (
    canonical_entity,
    canonical_number,
    canonical_ref,
    canonical_text,
)

#: The published bench seed (2026-06-10).
DEFAULT_SEED = 20260610

#: Corruption types, in deterministic cycling order (answer i gets type i % 7).
CORRUPTION_TYPES: tuple[str, ...] = (
    "sku_mutate",
    "price_change",
    "quote_distort",
    "entity_variant",
    "ean_mutate",
    "url_wrong_path",
    "warranty_months_change",
)

# --------------------------------------------------------------- word lists

_NAME_PREFIXES: tuple[str, ...] = (
    "Aqua", "Hydro", "Thermo", "Electro", "Turbo", "Mega", "Ultra", "Micro",
    "Robo", "Sono", "Aero", "Cryo", "Magno", "Opti", "Piezo", "Volta",
    "Dyna", "Ferro", "Gyro", "Helio",
)
_NAME_SUFFIXES: tuple[str, ...] = (
    "Pump", "Filter", "Valve", "Drill", "Mixer", "Sensor", "Heater",
    "Cooler", "Blower", "Cutter", "Welder", "Grinder", "Sander", "Router",
    "Presser", "Sealer", "Washer", "Drier", "Charger", "Mower",
)
_SKU_TAIL_LETTERS = "BCDFGHJKMNPQSTVWXZ"
_WARRANTY_CHOICES: tuple[int, ...] = (6, 12, 18, 24, 36, 48, 60)
_RETURN_DAY_CHOICES: tuple[int, ...] = (14, 21, 30, 45, 60)
_REFUND_DAY_CHOICES: tuple[int, ...] = (5, 7, 10, 15)

_QUOTE_OPENINGS: tuple[str, ...] = (
    "Always disconnect the appliance before",
    "Switch off the device before",
    "Unplug the unit before",
    "Power down the equipment before",
)
_QUOTE_MAINTENANCE: tuple[str, ...] = (
    "cleaning", "servicing", "inspection", "maintenance", "descaling", "storage",
)
_QUOTE_COMPONENTS: tuple[str, ...] = (
    "protective cover", "safety guard", "inlet screen",
    "sealing ring", "ground clamp", "drain cap",
)
#: Words substituted into distorted quotes — by construction these words
#: appear NOWHERE in any generated corpus document, so a distorted quote can
#: never accidentally remain a verbatim substring of the corpus.
_QUOTE_DISTORT_WORDS: tuple[str, ...] = (
    "guaranteed", "approved", "optional", "certified", "insured",
)

_URL_BRANDS: tuple[str, ...] = ("acmetools", "brightline", "nordwerk", "polartec")
_URL_PATHS: tuple[str, ...] = ("help", "contact", "warranty", "service")
#: Disjoint from _URL_PATHS — a wrong-path URL is never the real one.
_URL_WRONG_PATHS: tuple[str, ...] = ("claims", "tickets", "refunds-portal", "faq")

#: Mutated warranty durations: all odd, disjoint from _WARRANTY_CHOICES.
_WARRANTY_MUTATIONS: tuple[int, ...] = (5, 7, 9, 11, 13, 25, 27, 33, 35, 39, 51, 75)

#: Price mutation offsets, in euro cents (applied to the true price).
_PRICE_DELTAS_CENTS: tuple[int, ...] = (
    100, 250, 500, 1000, 1500, 2000, 2500, 5000, -100, -250, -500, -1000,
)


# -------------------------------------------------------------- model types


@dataclass(frozen=True)
class Product:
    """One synthetic catalog product (all values are grounded facts)."""

    name: str            # "AquaPump 3470" — CamelCase compound + model number
    model_number: int
    sku: str             # "AP-3470-K" — letters-digits-letter
    ean13: str           # 13 digits, valid GS1 checksum
    price_eur: str       # "249.99" — always 2 decimals
    warranty_months: int
    power_w: int
    quote: str           # the quoted safety sentence (without quote marks)


@dataclass(frozen=True)
class CorpusModel:
    """The full in-memory corpus model: products + global policy facts."""

    products: tuple[Product, ...]
    support_url: str
    return_days: int
    refund_days: int


@dataclass(frozen=True)
class Injected:
    """The single lie carried by a corrupted answer.

    `value` is the exact mutated string as it appears in the answer text;
    `canonical` is the canonical key the verification engine will assign to
    the corresponding atom (used by bench/run.py to assert the engine
    flagged *this* lie, not merely *something*).
    """

    value: str
    canonical: str


@dataclass(frozen=True)
class Answer:
    """One benchmark answer: clean, or corrupted by exactly one lie."""

    text: str
    label: str                     # 'clean' or a CORRUPTION_TYPES entry
    injected: Injected | None      # None iff label == 'clean'


# ----------------------------------------------------------------- EAN-13


def ean13_check_digit(digits12: str) -> int:
    """GS1 check digit for the first 12 digits of an EAN-13."""
    total = sum(int(d) * (3 if i % 2 else 1) for i, d in enumerate(digits12))
    return (10 - total % 10) % 10


def _make_ean13(rng: random.Random, used: set[str]) -> str:
    while True:
        body = "4" + "".join(str(rng.randrange(10)) for _ in range(11))
        ean = body + str(ean13_check_digit(body))
        if ean not in used:
            used.add(ean)
            return ean


# ------------------------------------------------------------ corpus model


def generate_corpus(rng: random.Random, n_products: int) -> CorpusModel:
    """Generate the in-memory corpus model — pure function of `rng`."""
    combos = [(p, s) for p in _NAME_PREFIXES for s in _NAME_SUFFIXES]
    if not 0 < n_products <= len(combos):
        raise ValueError(f"n_products must be in 1..{len(combos)}, got {n_products}")
    pairs = rng.sample(combos, n_products)
    # Model numbers avoid the year-shaped 1900-2099 band (the number
    # extractor treats standalone years as date atoms — keep the synthetic
    # data unambiguous by construction, not by luck).
    number_pool = list(range(1000, 1900)) + list(range(2100, 10000))
    model_numbers = rng.sample(number_pool, n_products)

    used_eans: set[str] = set()
    products: list[Product] = []
    for (prefix, suffix), model_number in zip(pairs, model_numbers, strict=True):
        name = f"{prefix}{suffix} {model_number}"
        sku = f"{prefix[0]}{suffix[0]}-{model_number}-{rng.choice(_SKU_TAIL_LETTERS)}"
        ean = _make_ean13(rng, used_eans)
        price = f"{rng.randint(10, 949)}.{rng.randrange(100):02d}"
        quote = (
            f"{rng.choice(_QUOTE_OPENINGS)} {rng.choice(_QUOTE_MAINTENANCE)} "
            f"and never operate it without the {rng.choice(_QUOTE_COMPONENTS)} fitted."
        )
        products.append(
            Product(
                name=name,
                model_number=model_number,
                sku=sku,
                ean13=ean,
                price_eur=price,
                warranty_months=rng.choice(_WARRANTY_CHOICES),
                power_w=rng.randrange(50, 2500, 10),
                quote=quote,
            )
        )

    support_url = (
        f"https://support.{rng.choice(_URL_BRANDS)}.example/{rng.choice(_URL_PATHS)}"
    )
    return CorpusModel(
        products=tuple(products),
        support_url=support_url,
        return_days=rng.choice(_RETURN_DAY_CHOICES),
        refund_days=rng.choice(_REFUND_DAY_CHOICES),
    )


# ------------------------------------------------------------ corpus files


def _catalog_text(model: CorpusModel) -> str:
    blocks = ["# Product catalog\n"]
    for p in model.products:
        blocks.append(
            f"## {p.name} (SKU {p.sku})\n"
            f"Power: {p.power_w} W. Price: €{p.price_eur}. "
            f"Warranty: {p.warranty_months} months.\n"
            f"EAN: {p.ean13}.\n"
            f'Safety note: "{p.quote}"\n'
        )
    return "\n".join(blocks)


def _csv_text(model: CorpusModel) -> str:
    lines = ["sku,name,price_eur,warranty_months"]
    for p in model.products:
        lines.append(f"{p.sku},{p.name},{p.price_eur},{p.warranty_months}")
    return "\n".join(lines) + "\n"


def _policy_text(model: CorpusModel) -> str:
    return (
        "# Support policy\n\n"
        f"Returns are accepted within {model.return_days} days. "
        f"Refunds are processed within {model.refund_days} days.\n"
        f"Support is available at {model.support_url}.\n"
    )


def _document_texts(model: CorpusModel) -> tuple[str, str, str]:
    """The exact texts written by :func:`write_corpus_files` (also used to
    assert that distorted quotes are not substrings of any document)."""
    return _catalog_text(model), _csv_text(model), _policy_text(model)


def write_corpus_files(model: CorpusModel, folder: Path) -> None:
    """Render the model into a trusted-corpus folder (deterministic bytes)."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "catalog.md").write_text(_catalog_text(model), encoding="utf-8")
    (folder / "products.csv").write_text(_csv_text(model), encoding="utf-8")
    (folder / "policy.md").write_text(_policy_text(model), encoding="utf-8")


# ------------------------------------------------------- grounded canonicals


def _grounded_canonicals(model: CorpusModel) -> dict[str, frozenset[str]]:
    """Canonical keys of every grounded value, by registry, plus 'all'."""
    refs: set[str] = {canonical_ref(model.support_url)}
    numbers: set[str] = {
        f"unit:day:{model.return_days}",
        f"unit:day:{model.refund_days}",
    }
    entities: set[str] = set()
    quotes: set[str] = set()
    for p in model.products:
        refs.add(canonical_ref(p.sku))
        refs.add(canonical_ref(p.ean13))
        numbers.add(f"money:EUR:{canonical_number(p.price_eur)}")
        numbers.add(f"unit:month:{p.warranty_months}")
        numbers.add(f"unit:w:{p.power_w}")
        numbers.add(f"integer:{p.model_number}")
        numbers.add(f"integer:{p.ean13}")
        entities.add(canonical_entity(p.name))
        quotes.add(canonical_text(p.quote))
    return {
        "refs": frozenset(refs),
        "numbers": frozenset(numbers),
        "entities": frozenset(entities),
        "quotes": frozenset(quotes),
        "all": frozenset(refs | numbers | entities | quotes),
    }


def _assert_novel(canonical: str, grounded: dict[str, frozenset[str]], what: str) -> None:
    assert canonical not in grounded["all"], (
        f"injected {what} collides with a grounded value: {canonical!r}"
    )


# ------------------------------------------------------- sentence templates


def _s_catalog(p: Product) -> str:
    return f"The {p.name} (SKU {p.sku}) costs €{p.price_eur}."


def _s_warranty(p: Product) -> str:
    return f"It is covered for {p.warranty_months} months."


def _s_quote(p: Product) -> str:
    return f'The manual states: "{p.quote}"'


def _s_ean(p: Product) -> str:
    return f"Its barcode is {p.ean13}."


def _s_power(p: Product) -> str:
    return f"The {p.name} draws {p.power_w} W."


def _s_returns(m: CorpusModel) -> str:
    return f"Returns are accepted within {m.return_days} days."


def _s_refunds(m: CorpusModel) -> str:
    return f"Refunds are processed within {m.refund_days} days."


def _s_support(m: CorpusModel) -> str:
    return f"Support is available at {m.support_url}."


#: slot name -> sentence builder (product slots take p, policy slots take m).
_PRODUCT_SLOTS = ("warranty", "quote", "ean", "power")
_POLICY_SLOTS = ("returns", "refunds", "support")
_ALL_SLOTS = ("catalog",) + _PRODUCT_SLOTS + _POLICY_SLOTS

#: For each corruption type: the slot whose honest sentence the lie replaces
#: (companions exclude it, so the answer carries EXACTLY one claim per slot).
_CORRUPTED_SLOT: dict[str, str] = {
    "sku_mutate": "catalog",
    "price_change": "catalog",
    "quote_distort": "quote",
    "entity_variant": "catalog",
    "ean_mutate": "ean",
    "url_wrong_path": "support",
    "warranty_months_change": "warranty",
}


def _slot_sentence(slot: str, p: Product, m: CorpusModel) -> str:
    builders = {
        "catalog": lambda: _s_catalog(p),
        "warranty": lambda: _s_warranty(p),
        "quote": lambda: _s_quote(p),
        "ean": lambda: _s_ean(p),
        "power": lambda: _s_power(p),
        "returns": lambda: _s_returns(m),
        "refunds": lambda: _s_refunds(m),
        "support": lambda: _s_support(m),
    }
    return builders[slot]()


# --------------------------------------------------------------- mutations


def _mutate_one_digit(
    rng: random.Random,
    digits: str,
    is_novel,
) -> str:
    """Change exactly one digit (never creating a leading zero); the first
    candidate accepted by `is_novel` wins. Deterministic via `rng`."""
    positions = list(range(len(digits)))
    rng.shuffle(positions)
    for pos in positions:
        replacements = [d for d in "0123456789" if d != digits[pos]]
        if pos == 0:
            replacements = [d for d in replacements if d != "0"]
        rng.shuffle(replacements)
        for d in replacements:
            candidate = digits[:pos] + d + digits[pos + 1 :]
            if is_novel(candidate):
                return candidate
    raise AssertionError(f"no novel single-digit mutation of {digits!r}")


def _lie_sku(rng: random.Random, p: Product, g: dict) -> tuple[str, Injected]:
    prefix, digits, tail = p.sku.split("-")
    mutated = _mutate_one_digit(
        rng, digits,
        lambda d: canonical_ref(f"{prefix}-{d}-{tail}") not in g["refs"],
    )
    bad_sku = f"{prefix}-{mutated}-{tail}"
    injected = Injected(value=bad_sku, canonical=canonical_ref(bad_sku))
    _assert_novel(injected.canonical, g, "sku")
    sentence = f"The {p.name} (SKU {bad_sku}) costs €{p.price_eur}."
    return sentence, injected


def _lie_price(rng: random.Random, p: Product, g: dict) -> tuple[str, Injected]:
    euros, cents = p.price_eur.split(".")
    true_cents = int(euros) * 100 + int(cents)
    deltas = list(_PRICE_DELTAS_CENTS)
    rng.shuffle(deltas)
    for delta in deltas:
        total = true_cents + delta
        if total < 100:
            continue
        bad_price = f"{total // 100}.{total % 100:02d}"
        canonical = f"money:EUR:{canonical_number(bad_price)}"
        if canonical not in g["numbers"]:
            injected = Injected(value=f"€{bad_price}", canonical=canonical)
            _assert_novel(canonical, g, "price")
            sentence = f"The {p.name} (SKU {p.sku}) costs €{bad_price}."
            return sentence, injected
    raise AssertionError(f"no novel price mutation of {p.price_eur!r}")


def _lie_quote(
    rng: random.Random, p: Product, g: dict, doc_canonicals: tuple[str, ...]
) -> tuple[str, Injected]:
    words = p.quote.rstrip(".").split()
    # Replace one INNER content word (length > 3 keeps articles intact).
    candidates = [i for i in range(1, len(words) - 1) if len(words[i]) > 3]
    idx = rng.choice(candidates)
    words[idx] = rng.choice(_QUOTE_DISTORT_WORDS)
    distorted = " ".join(words) + "."
    canonical = canonical_text(distorted)
    injected = Injected(value=distorted, canonical=canonical)
    _assert_novel(canonical, g, "quote")
    # Stronger than set membership: the engine matches quotes by substring
    # against whole-document canonicals — assert the distortion is absent.
    for doc in doc_canonicals:
        assert canonical not in doc, "distorted quote still a corpus substring"
    sentence = f'The manual states: "{distorted}"'
    return sentence, injected


def _lie_entity(rng: random.Random, p: Product, g: dict) -> tuple[str, Injected]:
    compound = p.name.rsplit(" ", 1)[0]
    mutated = _mutate_one_digit(
        rng, str(p.model_number),
        lambda d: canonical_entity(f"{compound} {d}") not in g["entities"],
    )
    variant = f"{compound} {mutated}"
    injected = Injected(value=variant, canonical=canonical_entity(variant))
    _assert_novel(injected.canonical, g, "entity")
    sentence = f"The {variant} ships with a {p.warranty_months}-month warranty."
    return sentence, injected


def _lie_ean(rng: random.Random, p: Product, g: dict) -> tuple[str, Injected]:
    mutated = _mutate_one_digit(
        rng, p.ean13, lambda d: canonical_ref(d) not in g["refs"]
    )
    injected = Injected(value=mutated, canonical=canonical_ref(mutated))
    _assert_novel(injected.canonical, g, "ean")
    sentence = f"Its barcode is {mutated}."
    return sentence, injected


def _lie_url(rng: random.Random, m: CorpusModel, g: dict) -> tuple[str, Injected]:
    base = m.support_url.rsplit("/", 1)[0]
    paths = list(_URL_WRONG_PATHS)
    rng.shuffle(paths)
    for path in paths:
        bad_url = f"{base}/{path}"
        canonical = canonical_ref(bad_url)
        if canonical not in g["refs"]:
            injected = Injected(value=bad_url, canonical=canonical)
            _assert_novel(canonical, g, "url")
            sentence = f"Support is available at {bad_url}."
            return sentence, injected
    raise AssertionError("no novel wrong-path URL")


def _lie_warranty(rng: random.Random, p: Product, g: dict) -> tuple[str, Injected]:
    months = list(_WARRANTY_MUTATIONS)
    rng.shuffle(months)
    for n in months:
        canonical = f"unit:month:{n}"
        if canonical not in g["numbers"]:
            injected = Injected(value=f"{n} months", canonical=canonical)
            _assert_novel(canonical, g, "warranty")
            sentence = f"It is covered for {n} months."
            return sentence, injected
    raise AssertionError("no novel warranty mutation")


# ----------------------------------------------------------------- answers


def _clean_answer(rng: random.Random, model: CorpusModel) -> Answer:
    p = rng.choice(model.products)
    slots = ["catalog"] + rng.sample(_PRODUCT_SLOTS, 2) + [rng.choice(_POLICY_SLOTS)]
    text = " ".join(_slot_sentence(s, p, model) for s in slots)
    return Answer(text=text, label="clean", injected=None)


def _corrupted_answer(
    rng: random.Random,
    model: CorpusModel,
    kind: str,
    g: dict,
    doc_canonicals: tuple[str, ...],
) -> Answer:
    p = rng.choice(model.products)
    if kind == "sku_mutate":
        lie, injected = _lie_sku(rng, p, g)
    elif kind == "price_change":
        lie, injected = _lie_price(rng, p, g)
    elif kind == "quote_distort":
        lie, injected = _lie_quote(rng, p, g, doc_canonicals)
    elif kind == "entity_variant":
        lie, injected = _lie_entity(rng, p, g)
    elif kind == "ean_mutate":
        lie, injected = _lie_ean(rng, p, g)
    elif kind == "url_wrong_path":
        lie, injected = _lie_url(rng, model, g)
    elif kind == "warranty_months_change":
        lie, injected = _lie_warranty(rng, p, g)
    else:
        raise ValueError(f"unknown corruption type: {kind}")

    pool = [s for s in _ALL_SLOTS if s != _CORRUPTED_SLOT[kind]]
    companions = [_slot_sentence(s, p, model) for s in rng.sample(pool, 2)]
    sentences = list(companions)
    sentences.insert(rng.randrange(len(sentences) + 1), lie)
    return Answer(text=" ".join(sentences), label=kind, injected=injected)


def generate_answers(
    rng: random.Random,
    model: CorpusModel,
    n_clean: int,
    n_corrupted: int,
) -> list[Answer]:
    """Generate `n_clean` grounded answers then `n_corrupted` answers each
    carrying exactly one injected lie (types cycled deterministically)."""
    g = _grounded_canonicals(model)
    doc_canonicals = tuple(canonical_text(t) for t in _document_texts(model))
    answers = [_clean_answer(rng, model) for _ in range(n_clean)]
    for i in range(n_corrupted):
        kind = CORRUPTION_TYPES[i % len(CORRUPTION_TYPES)]
        answers.append(_corrupted_answer(rng, model, kind, g, doc_canonicals))
    return answers
