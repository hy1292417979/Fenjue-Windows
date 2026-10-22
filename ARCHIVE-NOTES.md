# Source archive prepared 2026-10-22

This snapshot preserves the local Fenjue source and its existing Git history, including source changes that had not previously been committed. It is an archive of the current implementation, not a new verified release.

The source installation was `C:\Users\admin\Fenjue-Windows`. Local OAuth credentials, API keys, runtime settings, logs, databases, virtual environments, downloaded dependencies, build caches, and retry-proxy state are excluded. Restore those locally through the application's installation and account setup; do not commit them.

The archived source contains Cockpit-selected account routing and a legacy pinned-account path. This archive task preserves those implementations as found; it does not deploy or re-verify their behavior.

During the latest read-only check, port 17878 was not listening and Cockpit's Fenjue status endpoint returned 404. This archive does not establish that the installed Cockpit version is compatible with the patch.

Before removing the source installation, create a private GitHub repository, push this history, and verify the remote commit and tracked-file contents. Repository creation and upload have not yet succeeded in the restricted environment. Local source and local account data must not be deleted before that verification.
