# AGENTS.md

## Purpose
This repository is a small Flask app for monitoring RSCM and MesoSPIM datasets.
The codebase is simple, hand-maintained, and mostly configuration-by-convention.
Prefer small, local edits over broad rewrites.

## Instruction Sources
Checked during analysis:

- `AGENTS.md`: not present before this file was created.
- `.cursor/rules/`: not present.
- `.cursorrules`: not present.
- `.github/copilot-instructions.md`: not present.

There are no repository-local Cursor or Copilot rules beyond this file.

## Repository Layout
- `README.md`: short runbook for the lab server.
- `micro_status_flask/app.py`: Flask app, routes, and startup.
- `micro_status_flask/auth.py`: login manager setup, LDAP auth, and auth routes.
- `micro_status_flask/models.py`: SQLAlchemy models and shared `db` object.
- `micro_status_flask/forms.py`: WTForms definitions.
- `micro_status_flask/settings.py`: small constant-style settings module.
- `micro_status_flask/templates/`: Jinja templates.
- `micro_status_flask/data.db`: local SQLite file exists, but app config points to external storage paths.

## Environment Notes
The README documents the intended workflow on the lab server:

```bash
ssh lab@slogin.cbiserver.pitt.edu
conda activate microstatus-flask
cd ~/src/micro_status_flask/micro_status_flask
python app.py
```

Observed locally while analyzing this repo:

- The shell here has `python3`, not `python`.
- `flask` is not installed in the default environment here.
- `pytest` is not installed in the default environment here.
- Startup also depends on external packages and config, including `flask_file_browser.routes.settings`.

## Build And Run Commands
There is no formal build pipeline.
Use these as the closest repository-native commands.

- Run from the repo root: `python3 micro_status_flask/app.py`
- Run from inside `micro_status_flask/`, matching the README: `python app.py`

Caveats:

- App startup fails without Flask and related runtime dependencies.
- Auth setup imports `flask_file_browser.routes.settings`.
- The app uses absolute paths under `/CBI_FastStore/...` and `/h20/home/...`.

## Validation Commands
No lint, format, or test automation is configured in tracked repo files.
The safest built-in syntax check is `python3 -m compileall micro_status_flask`.
This succeeded during analysis.

## Lint Commands
There is no configured linter.
Do not claim `ruff`, `flake8`, `pylint`, or `mypy` are standard project commands unless you add and document them.
For small changes, use `python3 -m compileall micro_status_flask`.

## Test Commands
There is no tracked test suite today.
No `tests/` directory, `pytest.ini`, `pyproject.toml`, `tox.ini`, or unittest modules were found.

Current state:

- `python3 -m pytest` fails in the default environment because `pytest` is not installed.
- There are no repository tests to run even if pytest is installed.

If you add tests, prefer `pytest` and use these command shapes:

- Run all tests: `python3 -m pytest`
- Run one file: `python3 -m pytest tests/test_file.py`
- Run one test function: `python3 -m pytest tests/test_file.py::test_name -q`
- Run one test method: `python3 -m pytest tests/test_file.py::TestClass::test_name -q`

If you add tests, place them in a top-level `tests/` directory unless the change clearly needs another structure.

## Code Style
The project is plain Python + Flask + SQLAlchemy + WTForms.
Existing style is straightforward and mostly hand-formatted.
Match existing structure first, then improve clarity where you touch code.

## Imports
- Keep imports at module top by default.
- A local import inside a function is acceptable to avoid circular or environment-sensitive imports. `auth.py` already does this in `setup_auth`.
- Group imports as standard library, third-party, then local modules.
- Separate import groups with one blank line.
- Prefer explicit imports over wildcard imports.
- Remove unused imports when editing a file.
- For multiline imports, use parentheses instead of backslashes.

## Formatting
- Use 4-space indentation.
- Keep lines reasonably short for readability.
- Keep blank lines between logical blocks when it improves scanning.
- Keep two blank lines between top-level functions and classes.
- Prefer simple control flow over dense one-liners.
- Use ASCII unless the file already needs other characters.

## Naming
- Use `snake_case` for functions, variables, and helpers.
- Use `PascalCase` for classes such as models and forms.
- Use `UPPER_SNAKE_CASE` for constants, especially in `settings.py`.
- Route function names should describe the action, for example `edit_dataset` and `create_dataset`.
- Keep template names aligned with the route purpose.

## Types
- The current codebase does not use type hints.
- Do not add broad type-annotation churn to untouched files.
- Add targeted type hints only when they materially clarify new logic.

## Flask And Data Conventions
- Keep route handlers in `app.py` unless auth-specific behavior belongs in `auth.py`.
- Keep models in `models.py` and reuse the shared `db` object.
- Preserve table and column names that map to the existing database schema.
- Keep form field names aligned with model attributes so `form.populate_obj(...)` continues to work.
- Prefer ORM queries for application logic unless raw SQL is clearly necessary.
- If raw SQL is necessary, parameterize inputs instead of string-building query conditions.
- Protect mutating routes with `@login_required`.
- Follow POST-redirect-GET after successful form submissions.
- Use `flash(...)` for user-visible success and failure messages.
- Keep template context explicit.

## Error Handling
- Catch specific exceptions, not bare `except:`.
- Do not silently swallow database, LDAP, or subprocess failures.
- Validate external inputs before using them in queries, filesystem paths, or subprocess arguments.
- Log or print only when there is a concrete debugging benefit.

## Security And Config
- Do not hardcode new secrets, credentials, or environment-specific absolute paths.
- If config must change, prefer explicit settings over embedding values in route code.
- Preserve list-style subprocess arguments; do not introduce `shell=True` unless explicitly required.
- Treat auth and rate-limiting code as security-sensitive and make minimal changes there.

## Verification Sequence
For most small Python changes in this repo:

1. Edit the minimal set of files.
2. Run `python3 -m compileall micro_status_flask`.
3. If the proper environment exists and startup is relevant, run `python3 micro_status_flask/app.py`.
4. If you add tests, run the narrowest relevant `pytest` command first, then the full suite.

## Known Gaps
- No dependency manifest is tracked.
- No automated tests are tracked.
- No lint or formatting tool is tracked.
- Some runtime configuration depends on external lab infrastructure.

Agents should state these gaps plainly rather than inventing missing tooling.
