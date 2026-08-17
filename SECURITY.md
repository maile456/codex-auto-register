# Security Policy

## Supported version

Security fixes are applied to the latest commit on the default branch.

## Reporting a vulnerability

Do not open a public issue containing credentials, tokens, account data,
mailbox URLs, proxy URLs, browser debugging addresses, payment links or logs
with unredacted request/response bodies.

Use GitHub private vulnerability reporting when it is enabled for the
repository. Include the affected component, reproduction steps, impact and a
minimal redacted log excerpt.

## Deployment boundary

The project is designed for local Windows use. FastAPI, Vite, MailCom Hub,
MongoDB, RoxyBrowser OpenAPI and sidecars should remain bound to loopback by
default. Any remote exposure requires authentication, TLS, firewall rules and
rate limiting.

Never commit these paths or values:

- `.env` and `data/`
- MongoDB/SQLite databases and JSONL logs
- email or proxy credentials
- Access/Refresh/Session tokens and TOTP secrets
- HeroSMS, RoxyBrowser, SSH or management keys
- browser CDP/WebSocket endpoints

Before publishing a release, inspect the staged Git tree rather than only the
working directory.
