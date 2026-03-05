# Security Policy

## Supported Versions

As this project is in early release, security fixes are applied to the latest version only.

| Version | Supported |
|---|---|
| latest (main) | ✅ |
| older commits | ❌ |

## Reporting a Vulnerability

**Please do not report security vulnerabilities through public GitHub issues.**

If you discover a security vulnerability, please report it responsibly by emailing:

**sachin@prayog.io**

Include as much detail as possible:
- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Suggested fix (if any)

You will receive a response within 48 hours. We will work with you to understand and address the issue before any public disclosure.

## Scope

Security concerns relevant to this project include:

- **Arbitrary code execution** via MCP tool inputs
- **Path traversal** in file-reading tools (`get_node`, `get_code`, `get_signature`)
- **Credential exposure** via session logs or graph nodes
- **Malicious graph YAML** that causes unintended behavior when loaded

## Out of Scope

- Vulnerabilities in third-party dependencies (report those to the dependency maintainer)
- Issues in the user's own project files processed by the indexer
