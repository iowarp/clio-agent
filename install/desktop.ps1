# CLIO Desktop, from a URL:
#
#   irm https://clio.iowarp.ai/desktop.ps1 | iex
#
# The same as `install.ps1 -Desktop`; the website serves both files from the same commit
# (.github/workflows/pages.yml). Any arguments go through (`-Dev`, `-Version <v>`).
& ([scriptblock]::Create((Invoke-RestMethod -UseBasicParsing 'https://clio.iowarp.ai/install.ps1'))) -Desktop @args
