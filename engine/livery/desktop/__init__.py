"""Livery Desktop: the local engine behind the Electron app.

Projects live outside the application (see `paths.app_home`). Each holds immutable copies
of the imported Word files, the accepted design revisions, pending proposals, builds per
document and revision, and sanitized chat records. Agents never see a project directory:
they work in a separate, sanitized workspace that only holds synthetic content.
"""
