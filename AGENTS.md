# Public source distribution

- Use `python scripts/run_public.py setup` and `python scripts/run_public.py start`.
- Keep AI disabled unless the user explicitly enables it with their own credentials and budget.
- Never commit credentials, runtime databases, uploads, logs, or local build artifacts.
- Preserve existing budget records; do not reset usage or replace ledgers.
- Source provenance and the limits of verification are recorded in `PUBLICATION.md` and `SOURCE_PROVENANCE.json`.
- Do not represent build or startup checks as a completed business acceptance suite.
- Keep API and schema changes explicit, and preserve third-party license notices.
