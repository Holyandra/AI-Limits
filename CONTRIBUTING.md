# Contributing

Thanks for your interest in AI Limits. The application currently targets Windows and Python 3.12.

## Set up a development environment

From the project directory, run these commands in PowerShell:

```powershell
py -3.12 -m venv .venv
& ".\.venv\Scripts\python.exe" -m pip install --upgrade pip
& ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt
& ".\.venv\Scripts\python.exe" -m unittest -v
```

Provider tests use local fixtures and do not require signed-in accounts. Please add or update a test when changing quota parsing or display behavior.

## Keep private data out of commits

Do not commit API keys, OAuth or CSRF tokens, account data, local app caches, generated shortcuts, or screenshots containing live quota information. The `.gitignore` excludes common local artifacts, but review `git status` before committing.

Antigravity's quota service is an internal local interface and may change. Keep requests limited to the user's own local service and avoid logging authorization headers or raw account responses.

## Documentation and assets

Keep `README.md` and `README.ru.md` in sync when changing installation or user-facing behavior. See [`assets/brands/SOURCES.md`](assets/brands/SOURCES.md) for third-party artwork sources; project code licensing does not relicense those marks.
