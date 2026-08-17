# Vendored source

Source directory: `F:\OAI-PayPal-Extractor-Sanitized-20260813-142859\payment_link_extractor`

Package metadata:

- Name: OAI PayPal Extractor Sanitized
- Built: 2026-08-13T14:29:28
- Providers: PayPal, GoPay, GCash
- Checkout branches: OAICS and CS Checkout

The Python package is kept structurally intact so its relative imports and
checkout/provider behavior remain traceable. AutoRegister-specific FastAPI
adapters live outside this directory.

The source root's later-added `README.md` is preserved byte-for-byte as
`SOURCE_README.md` (SHA-256
`A7F0BEB38986EFC95E031B391AFC2A4DA01DC68C9C2BF4ADF8891ED5222D74E4`).
Its standalone Flask routes are exposed by the AutoRegister FastAPI adapter as
compatibility aliases while the existing namespaced routes remain available.
