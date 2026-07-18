# MKV Auto Threat Model

This document outlines the high-level threat model for MKV Auto.

It is not an exhaustive security audit, but a practical description of risks,
trust boundaries, and design assumptions.

---

## Trust Assumptions

- MKV Auto is installed by the user on infrastructure they control.
- The host system is trusted by the user.
- Users are responsible for securing their host environment.

---

## Assets

The primary assets MKV Auto is responsible for protecting are:

- Media metadata
- User configuration
- Application state
- Local system integrity

MKV Auto does not handle:
- financial data
- personal identity data
- authentication credentials for third-party services (beyond metadata access)

---

## Threat Actors

Considered threat actors include:
- malicious local users
- compromised containers
- accidental misconfiguration
- untrusted media files or disc content

Remote, internet-based attackers are not a primary threat model unless the user
explicitly exposes the service publicly.

---

## Attack Surfaces

### Web Interface
- HTTP endpoints
- API input validation
- File upload and metadata parsing

### Container Runtime
- Privileged helper component
- Mounted volumes
- Device passthrough (optical drives)

### External Tooling
- Media processing tools
- Disc ripping utilities

---

## Mitigations

- Minimal privilege separation between components
- Narrowly scoped privileged helper
- Explicit input validation
- No automatic execution of untrusted code
- Clear separation between configuration and code

---

## Out of Scope

The following are explicitly out of scope:
- Host OS hardening
- Network perimeter security
- Third-party dependency vulnerabilities beyond reasonable updates
- Modified or unofficial builds

---

## Security Philosophy

MKV Auto favors:
- explicit behavior over hidden automation
- transparency over obscurity
- predictable failure modes over silent recovery

Security is treated as an ongoing process, not a one-time guarantee.
