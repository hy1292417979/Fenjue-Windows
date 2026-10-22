# Source archive prepared 2026-10-22

This snapshot preserves the local Fenjue source and its existing Git history, including source changes that had not previously been committed. It is an archive of the current implementation, not a new verified release.

`Codex焚决.bat` preserves the installed desktop retry-proxy menu from 2026-10-14. `Codex焚决.原版.bat` preserves the earlier guard menu. The source installation itself did not contain the current desktop menu, so it was captured separately before cleanup.

The source installation was `C:\Users\admin\Fenjue-Windows`. Local OAuth credentials, API keys, runtime settings, logs, databases, virtual environments, downloaded dependencies, build caches, and retry-proxy state are excluded. Restore those locally through the application's installation and account setup; do not commit them.

The archived source contains Cockpit-selected account routing and a legacy pinned-account path. This archive task preserves those implementations as found; it does not deploy or re-verify their behavior.

During the latest read-only check, port 17878 was not listening and Cockpit's Fenjue status endpoint returned 404. This archive does not establish that the installed Cockpit version is compatible with the patch.

The private repository is https://github.com/hy1292417979/Fenjue-Windows. Before removing the source installation, push the final snapshot and verify the remote commit, Git tree, repository visibility, and all tracked-file blob hashes. Move the local installation and its local account data to the Windows Recycle Bin only after that verification; account data is deliberately not uploaded.
