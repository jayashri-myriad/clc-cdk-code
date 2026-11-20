# Standalone CLC analysis

This sample installs the CLC Server Command Line Tools if needed and runs a
paired-read cloud workflow synchronously against an existing CLC server.

## Setup

Use Python 3.10+ and Bash on Linux (Bash is only needed for installation).
Install the Python dependency from the repository root:

```sh
python3 -m pip install python-dotenv==1.1.1
```

This dependency is also included in the repository's [requirements.txt](../requirements.txt).

Configure [.env](./.env) inside this folder. For a fresh checkout, copy
[env.example](./env.example) to `.env`. Set the host, port, user, password,
workflow, preset, installer, installation directory, and input file there.
The password is deliberately blank: supply it locally or export `CLC_PASSWORD`
through your approved Vault integration. Shell variables override `.env`.
Values are literal (no `${VARIABLE}` expansion); quote values containing spaces
or `#`. Prefer a least-privileged service account.

Relative configured paths resolve from this folder, not the working directory.
Absolute paths also work. Keep the compatible installer in the configured
location. The default connection expects a local tunnel on `127.0.0.1:7778`.
CLC must be able to read the input S3 objects and write the destination prefix.

Set `read1`, `read2`, and `destination` in the configured input JSON.
See [input.example.json](./input.example.json) for the format. Use distinct
paired FASTQ URLs and an S3 destination prefix ending in `/`. The runner checks
that these fields are non-empty strings; the CLC client validates their values.
Do not commit credentials or sample-identifying paths. The local `.env`,
input, installer, and installation directory are ignored by Git.

## Run

From the repository root:

```sh
python3 clc-client-sample/run.py
```

The client is installed once and reused. An incomplete installation is not
overwritten: inspect it before retrying. To override the installer:

```sh
python3 clc-client-sample/run.py /path/to/installer.sh
```

The runner waits for CLC to finish and propagates its exit status. It does not
retry or submit asynchronously. On failure or interruption, check CLC job
history and restricted server logs before retrying to avoid duplicate jobs.
Raw output is suppressed because it can contain sensitive data; for synthetic,
non-sensitive data only, stream it live with:

```sh
python3 clc-client-sample/run.py --show-test-output
```

The CLI requires the password in `-W`, so it is visible in process arguments
while running. Use an isolated host, restrict access to `.env`, and never
commit it. The runner does not print the command or password.

The legacy [run.sh](./run.sh) is not used by this runner and does not load
`.env`; use the Python command above.

## Test

Tests use a temporary configuration and mocked processes; no CLC jobs are submitted.

```sh
python3 clc-client-sample/test_run.py
```
