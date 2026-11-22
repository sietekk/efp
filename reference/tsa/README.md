# Pinned TSA trust anchors

`efp manifest --verify` always checks that a token was granted and that its message imprint is the
SHA-256 of the manifest file as it stands. It checks the **signature chain** too when PEM files
(`*.pem`) are present here; with none, the result says "signature NOT checked" rather than claiming
it was.

To enable the signature check, put the root (and any intermediate) certificates of the TSAs listed
in `config.toml` `[capture].tsa_urls` in this folder as `<name>.pem`, taking them from each TSA's own
site over HTTPS, and commit them. They are public certificates, not secrets. Review them once: a
trust anchor you did not verify is worse than none.
