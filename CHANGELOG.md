# Changelog

## 1.0.0 - 2026-10-09

- Added a persistent interactive home menu and guided folder selection.
- Improved preview, operation summaries, terminal compatibility, and CLI
  examples without adding runtime dependencies.
- Added confirmation for undo and clearer warnings for explicitly destructive
  overwrite operations.
- Versioned operation history, made history writes atomic, and hardened undo
  against malformed records, moved paths, and destination conflicts.
- Added focused filesystem, configuration, history, partial-failure, and
  interactive tests, plus GitHub Actions CI.
