# Third-Party Notices

This repository is licensed under the MIT License. The dependencies below are
separate projects governed by their own licenses. The declarations here do not
relicense them. Consult the installed package metadata and upstream repositories
for complete license texts and notices.

## Runtime

The core tool has no third-party runtime dependencies. It uses the Python
standard library only.

## Optional Gmail extra

Installed only with `pip install -r requirements-gmail.txt`:

- [google-auth-oauthlib](https://github.com/googleapis/google-auth-library-python-oauthlib),
  Apache License 2.0
- [google-api-python-client](https://github.com/googleapis/google-api-python-client),
  Apache License 2.0

## Development

- [pytest](https://github.com/pytest-dev/pytest), MIT License

## External services

vcoutreach can call external APIs when you supply your own credentials: the
Publik API, an OpenAI-compatible API, the Anthropic API, and the Gmail API. None
of their code is included here. Their use is governed by each provider's own
terms and pricing, which you accept directly with that provider.

## Data

No third-party source code, contact lists, generated drafts, or vendor data are
vendored in this repository. All names, organizations, products, and addresses in
the sample files are fictional, and any resemblance to real ones is coincidental.
