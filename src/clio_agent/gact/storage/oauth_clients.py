"""CLIO-owned public native OAuth application registrations.

These identify the installed application; they do not grant access to an account.
Google's Desktop client value is non-confidential installed-app metadata, included
for token exchange compatibility. User tokens remain in the private account vault.
Never put web-client secrets, service-account keys or user tokens in this module.

https://developers.google.com/identity/protocols/oauth2#installed
"""

GLOBUS_CLIENT_ID = "ca844cdc-ddb1-4332-bb66-69f9479d99b5"
GITHUB_CLIENT_ID = "Iv23lieBkZ0yxGbLAIyE"
GITHUB_APP_URL = "https://github.com/apps/clio-agent"
GOOGLE_CLIENT_ID = "209468117785-4uplk9getqe898gbvms8g0qiae62rmoo.apps.googleusercontent.com"
GOOGLE_CLIENT_SECRET = "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"
