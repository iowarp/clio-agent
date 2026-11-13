---
name: fixture-secret-guide
description: Use when checking the Fixture Secret test connector, to read what its server received.
---

The Fixture Secret connector's server reports, through `fixture_secret_check`, whether
it received its access token (as a short hash, never the token) and where it runs.
When the person names another host for it, set the connector's `where` value.
