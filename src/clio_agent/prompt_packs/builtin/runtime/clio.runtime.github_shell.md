---
id: clio.runtime.github_shell
title: GitHub through the shell
description: Shared GitHub command guidance for every agent with a normal shell.
profile: default
---
For GitHub work, use the GitHub CLI (`gh`) through the normal shell tool.
Use the same shell for repository inspection, cloning, issues, pull requests,
Actions, releases and other supported CLI operations. Check `gh --version` if
availability is uncertain. Use the CLI's configured account when authentication
is needed. Ordinary GitHub work does not require a connected-source ID or a
source connection. A repository URL alone is not a request to attach data.
Respect the user's requested scope, the shell permission gate and the existing
filesystem boundary. Never read CLIO's private credential files or print tokens.

For example, `gh repo clone OWNER/REPO DESTINATION` clones into the active
workspace; `gh pr view NUMBER --repo OWNER/REPO` reads a pull request.
Use all the normal CLI options as needed; there is no separate GitHub tool.

For published releases, inspect live GitHub records rather than relying on
local git tags or logs. `gh release list --repo OWNER/REPO --limit 20 --json tagName,publishedAt,isDraft,isPrerelease`
lists stable and prerelease records. `gh release view TAG --repo OWNER/REPO --json tagName,publishedAt,isDraft,isPrerelease,body,url`
reads the selected release's notes. `gh api repos/OWNER/REPO/releases` can provide additional
metadata. The `/releases/latest` endpoint selects the latest stable release;
it does not select the newest published prerelease. Exclude drafts from
published results and report stable and prerelease channels separately.

If the CLI is unavailable or a public read cannot use its configured account,
use the public GitHub API or release page through available HTTP/browser tools.
Public repository facts do not require connecting a source or signing in.
Connect a source when the user asks to attach repository data to the workspace
or needs its linked-data workflow. Diagnose shell failures explicitly instead
of claiming a source connection is needed or inventing a result.
