# llmapi

Declarative Fly.io deployment of CLIProxyAPI and CPA Manager Plus on a single
Tokyo (`nrt`) Machine, using the upstream Docker images.

## Deployment

This follows Fly.io's official GitHub Actions integration:
`actions/checkout`, `superfly/flyctl-actions/setup-flyctl`, and `flyctl deploy`.
No image build or remote builder is needed.

- A push to deployment files on `main` deploys the configuration.
- Every day at **04:19 Asia/Shanghai (20:19 UTC)** the workflow checks both
  `eceasy/cli-proxy-api:latest` and `seakee/cpa-manager-plus:latest`.
- If neither digest changed, it skips the deployment and restart.
- A manual **Run workflow** checks the images and deploys immediately.
- Both images are resolved and pinned by digest for each deployment.
- Runs are serialized. Deployments update only the existing Machine with
  `--ha=false --strategy immediate --update-only --only-machines`.
- A volume snapshot must be ready before deployment. It is a live-volume
  snapshot, not an application-level SQLite backup. Snapshots follow the
  volume's existing retention policy.
- Verification checks both images, service checks, HTTP endpoints, the original
  volume, resource sizes, startup commands, Secret mappings and autostop policy.

Single-Machine updates briefly interrupt active connections. If an update fails,
the workflow fails visibly in Actions. Database schema migrations mean that an
automatic binary downgrade is not always safe; the snapshot supports recovery.

## Preserved configuration

- One shared CPU, 256MB RAM, 512MB swap, Tokyo region.
- One existing 1GB `cpa_data` volume mounted at `/home`.
- HTTPS 443 to CPA on 8317; HTTPS 8443 to cpamp on 18317.
- Autostop disabled, autostart enabled, HTTP idle timeout 600 seconds.
- SIGTERM with 30 seconds to stop; TCP checks for both services.
- CPA reads `/home/cpa/config.yaml` directly.
- CPA currently runs a memory-budget experiment: `GOMEMLIMIT=128MiB`, `GOGC=50`.
  These are Go runtime settings, not a process-wide hard memory cap. Early real
  traffic showed lower heap/swap usage but still substantial I/O stalls on large
  requests; this is not a claim of long-term stability. Native plugin allocation
  is outside the Go budget. The image and Machine size were not changed.
- CPA uses the existing management Secret as `MANAGEMENT_PASSWORD`, its native
  constant-time authentication path. This avoids repeating bcrypt work on every
  management polling request; the key and authentication remain unchanged.
- cpamp polls every 2 seconds (`USAGE_POLL_INTERVAL_MS=2000`) instead of the
  default 500ms, with the original batch size of 100. Only reporting freshness
  is affected; API requests do not pass through the collector.
- cpamp's database and encryption key stay under `/home/cpamp/data`.
- cpamp diagnostics listen only on `127.0.0.1:18316`
  (`CPA_MANAGER_PPROF_ADDR`). No public service port exposes this listener.
  Keep it available for capturing intermittent stalls without another restart.
- cpamp's validated environment export is retained to avoid image-default path
  overrides; the remaining settings are native Fly declarations.

## Credentials and public files

`FLY_API_TOKEN` is a GitHub Actions repository Secret containing a deployment
token scoped to the `llmapi` app. It was created with `--expiry 0` at the owner's
request. Fly reports its expiration as **2126-09-26**, approximately 100 years,
rather than literal infinity. The original one-year token was revoked.

The actual `CPA_MANAGEMENT_KEY` and `CPA_MANAGER_ADMIN_KEY` remain in Fly app
Secrets. This repository contains their names only. Authentication files,
runtime configuration, databases, encryption keys, local backups and Machine
state exports must never be committed.

The workflow uses a standard GitHub-hosted Ubuntu runner. Public-repository
standard runners are free and do not consume private-repository included
minutes. GitHub can delay scheduled runs and disables public scheduled workflows
after 60 days without repository activity; keep this repository active or
re-enable the schedule in Actions when GitHub prompts you.

## References

- https://docs.fly.io/launch/continuous-deployment-with-github-actions
- https://docs.fly.io/machines/guides-examples/multi-container-machines
- https://docs.fly.io/reference/configuration
- https://docs.github.com/en/billing/concepts/product-billing/github-actions
