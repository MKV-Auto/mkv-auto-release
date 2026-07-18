# Development and architecture (release repo)

This document gives an overview of how MKV Auto is built and why certain design choices were made. It is **not** a contribution guide (the canonical development repo is separate). It is for anyone who has this source snapshot and wants to understand the system.

## Architecture overview

MKV Auto is an automated disc ripping and media management stack:

- **Frontend**: Angular app (built and served inside the container).
- **Backend**: FastAPI API plus Celery workers for rip, post-process, and transfer.
- **Data**: PostgreSQL (primary state) and Redis (Celery broker and result backend).
- **Privileged helper**: A small process that runs with elevated privileges for optical drive and MakeMKV operations.

All of these run in a **single container** in the standard Docker image. The image includes NGINX (reverse proxy), the API, workers, optional embedded PostgreSQL and Redis, and the root helper, managed by Supervisor under Tini.

## Single-container design

The image is a single-container deployment so that:

- **Deployment is simple**: One image, one process tree, one place for logs and data.
- **State is local**: Database and Redis are either embedded in the container or explicitly configured; there is no assumption of a pre-existing cluster.
- **Resource and lifecycle alignment**: One container to build, ship, and upgrade; version skew between frontend, API, and workers is avoided.

Multi-container setups (e.g. external PostgreSQL/Redis) are supported via configuration; the default remains one container for ease of use.

## Privileged mode and the root helper

The container is intended to run in **privileged mode** (or with the capabilities needed for optical drive access). That is required because:

- **Optical drive access**: Reading discs and controlling the drive (e.g. eject, autoclose) needs direct device and kernel access.
- **MakeMKV**: The ripping engine expects to work with block devices and may spawn helper processes that need access to the drive.
- **Mount operations**: Optional NFS/SMB transfer destinations may require mount/unmount from inside the container.

To keep the main API and workers from running everything as root, **privileged operations are isolated in a root helper**:

- A small separate process runs with the privileges needed for drive and MakeMKV operations.
- The API and workers talk to it over a Unix domain socket (e.g. mount, unmount, MakeMKV update).
- Only that helper runs with elevated privileges; the rest of the stack runs as normal services under Supervisor.

So: the **container** is privileged so the stack can see the drive; **inside** the container, only the root helper holds those privileges.

## Snapshot model

This repository is a **released snapshot** of the development source:

- The canonical development repository is private. Public releases are **sanitized snapshots**: only code and docs needed to build, run, and understand the release are published.
- Each tagged version (e.g. `v1.0.0`) corresponds to a specific snapshot and a container image. Not every internal change is released.
- Before publishing, the tree is checked for secrets, internal tooling, and personal or experimental content. The goal is a clean, auditable snapshot that respects privacy and security boundaries—not to hide how the system works.

So what you see here is a point-in-time, release-ready view of the project, not the live development branch.

## Design principles

- **Backend as source of truth**: Persistent state lives in PostgreSQL; the UI is stateless and reflects API and WebSocket updates.
- **Explicit workflows**: Rip → label (when needed) → post-process → transfer, with clear state and progression so jobs can be resumed and understood.
- **Single responsibility**: Services and components have focused roles; cross-cutting concerns (e.g. workflow orchestration) are centralized in designated services.
- **Recovery-friendly**: Jobs and state are designed so that restarts and failures can be detected and, where possible, resumed without manual fix-up.
- **Controlled privilege**: Only the root helper runs with elevated privileges; the rest of the stack uses normal process and user isolation.

These principles guide how the codebase is structured and how new behavior is added, even though this repo does not host the main contribution workflow.

---

For day-to-day use (running the container, setup, and operations), see the main [README](README.md) and the linked guides (Installation, Docker, Quick start).
