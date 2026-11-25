# Security

Report a security problem in private. On GitHub, open **Security**, then
**Advisories**, then **Report a vulnerability**. Do not open a public issue
that shows an API key, a secret or a local path.

The plug-in gets the Sharadar API key only from the stdin handshake of the
Meridian host. It never logs the key, and it binds only to `127.0.0.1`.

CI scans the Git history for secrets with gitleaks. It also fails when the
tree holds a symbolic link or a home folder path.

Fixes go to `main` and to the next release.
