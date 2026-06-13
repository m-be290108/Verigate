# Using VeriGate commercially

VeriGate is **open-core**: the code is source-available under
[FSL-1.1-ALv2](LICENSE) (it becomes Apache-2.0 two years after each release).
In plain words — and this is not legal advice, just a summary of the license:

## Do I need a commercial agreement?

| You are… | …doing this | Pay / contact? |
|---|---|---|
| A company | running VeriGate **for your own use** — verifying your own chatbot/RAG, even in production, even as a for-profit | **No.** Free to self-host. Use it, modify it, deploy it. |
| A vendor / agency | **embedding VeriGate inside a product or service you sell** to others (e.g. a legaltech assistant, a "verified AI" feature) | **Yes — contact me.** This is beyond the FSL's free grant; we agree commercial terms. |
| Anyone | reselling VeriGate, or offering a **competing** verification product built on its code | **Yes — contact me.** A Competing Use needs a separate license. |

If you only want to stop *your* AI from making things up, you owe nothing and
need to talk to no one. That's the point.

## What you can pay me for

The core is free. A commercial relationship buys you one or more of:

1. **Commercial / OEM license** — the right to embed or resell VeriGate inside
   your own product (the table above). This is the main path for legaltech and
   AI-agency partners.
2. **Support & SLA** — a guaranteed response time, a named contact, help with
   upgrades and corpus tuning.
3. **Services** — custom domain packs (your reference/identifier formats), help
   structuring your corpus for the closed-domain guarantee, deployment and
   integration.
4. **On the roadmap, on request** — a managed/hosted deployment, SSO/RBAC for
   teams. (Today the core ships the tamper-evident audit trail, provenance
   addressing, and the scoped + strict closed-domain mode — all free.)

## Indicative pricing

Starting points, not a fixed quote — tell me your case and I'll be precise:

- **Self-hosted community core** — free (FSL).
- **SME, on-prem, with support** — from €3,000–6,000 / year.
- **Enterprise, with support + services** — from €15,000–25,000 / year.
- **Embed / OEM license** — depends on your product and scale; let's talk.
- **Pilot** — €500–2,000, credited against year one. Paid pilots get you my
  full attention and an honest answer about whether this fits your case.

## Honest scope (so we don't waste each other's time)

VeriGate guarantees **facts, not judgment** — it verifies references, figures,
quotes and named entities against your corpus, deterministically and offline.
It does not catch every possible hallucination, and it works best on a bounded
domain where your data is the source of truth. Read
[docs/GUARANTEE.md](docs/GUARANTEE.md) before buying — if your case isn't a fit,
I'll tell you.

## Contact

**Mathieu Bellot** — mathieu.ballotma@gmail.com
(subject line `[VeriGate commercial]` gets you a faster reply).
