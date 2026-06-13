# Security Policy

VeriGate is a verification and audit tool — its trustworthiness is the product.
Security reports are taken seriously and handled with care.

## Reporting a vulnerability

**Please do not open a public issue for security vulnerabilities.**

Report privately via one of:

- GitHub's [private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
  (Security tab → "Report a vulnerability") — preferred.
- Email: mathieu.ballotma@gmail.com with `[VeriGate security]` in the subject.

Please include: affected version/commit, a description, and a minimal
reproduction (a failing test or a short script is ideal).

## What to expect

- Acknowledgement within **5 business days**.
- An initial assessment (confirmed / not-a-vuln / need-more-info) within
  **15 business days**.
- Coordinated disclosure: a fix and a public advisory are published together;
  credit is given to the reporter unless they prefer to remain anonymous.

## Scope — what counts as a vulnerability

Because of what VeriGate claims, these are in scope and treated as security
issues, not mere bugs:

- A way to make a **false atom verify** (a fabricated reference/figure/quote
  that the engine reports as `VERIFIED`) — beyond the *documented* limitations
  in the README and `DECISIONS.md` (e.g. membership-not-association, D-013).
- A way to **tamper with the audit trail undetected** — beyond the documented
  threat model (the HMAC secret being available to the attacker).
- Any **outbound network call** from the core engine, ingestion, or
  verification path (the "0 network" invariant is contractual and
  socket-guard-tested).
- A way to make a verification **report non-reproducible** for identical
  (corpus, answer) inputs.
- Path traversal, code execution, or data exfiltration via ingestion of a
  crafted document or a custom YAML pack.

## What is NOT a vulnerability

The documented limitations are by design, not security holes — please report
them as feature discussion, not vulnerabilities:

- Cross-attribution lies passing (a real value attributed to the wrong
  subject) — D-013, stated in the README.
- A lie expressed in plain prose with no checkable atom passing through.
- The default HMAC secret living next to the database (the threat model is
  documented; supply the secret out-of-band for stronger guarantees).
