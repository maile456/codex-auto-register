# Contributing

## Development setup

Follow the root README and keep all services bound to `127.0.0.1` while
developing. Use test fixtures instead of real credentials or account data.

## Change requirements

1. Keep changes scoped to one behavior.
2. Add or update tests for behavior changes.
3. Do not weaken secret redaction or local-only network defaults.
4. Preserve compatibility with Windows PowerShell and Python 3.13.
5. Document new environment variables in `.env.example` and the README.
6. Record third-party source and license information before vendoring code.

## Verification

```powershell
cd app
npm.cmd run type-check
npm.cmd test -- --run
npm.cmd run build
& ..\register_env\Scripts\python.exe -m pytest tests\backend -q

cd ..\mailcom-manager
& ..\register_env\Scripts\python.exe -m pytest tests -q
```

Run `git status --short` and `git diff --check` before committing. Review
`git ls-files` to ensure no runtime data or credentials were staged.

## Pull requests

Describe the problem, implementation, validation commands and any migration or
rollback steps. Screenshots and logs must use synthetic data.
