# Signed configuration

Keep the minisign private key outside this repository and outside the Caddy
container. After validating JSON locally, sign it with `../scripts/sign-config.sh`
and deploy both `NAME.json` and `NAME.json.minisig`. Point the image's URL template
at the desired file, for example `https://config.example/receiver-id.json`.
