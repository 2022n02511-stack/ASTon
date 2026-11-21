```python
 █████╗ ███████╗████████╗ ██████╗ ███╗   ██╗
██╔══██╗██╔════╝╚══██╔══╝██╔═══██╗████╗  ██║
███████║███████╗   ██║   ██║   ██║██╔██╗ ██║
██╔══██║╚════██║   ██║   ██║   ██║██║╚██╗██║
██║  ██║███████║   ██║   ╚██████╔╝██║ ╚████║
╚═╝  ╚═╝╚══════╝   ╚═╝    ╚═════╝ ╚═╝  ╚═══╝

 ██████╗██╗     ██╗
██╔════╝██║     ██║
██║     ██║     ██║
██║     ██║     ██║
╚██████╗███████╗██║
 ╚═════╝╚══════╝╚═╝
```

https://github.com/user-attachments/assets/812a1990-13c3-4c45-8a8e-080f602c7a3f

# ASTon

**Automated dependency migration, from breaking-change detection to a reviewable pull request.**

ASTon is a repository-native dependency migration tool for Python projects. It analyzes dependency updates, identifies breaking changes relevant to your code, generates migrations, verifies candidate changes, and prepares them for review.

Think of it as a Git-style workspace for dependency migrations: initialize ASTon in your repository, run migrations, inspect changes, and deliver them through a pull request.

[Quick Start](#quick-start) · [Demo](#demo) · [How It Works](#how-it-works) · [CLI Reference](#cli-reference) · [Architecture](#architecture) · [Roadmap](#roadmap)

**Making dependency migrations less manual.**

---

## The problem

A new library version ships. Its APIs change, old methods are deprecated or removed, and simply upgrading the dependency can break your application.

For example, the OpenAI Python SDK changed from:

```python
# Legacy API
import openai

response = openai.ChatCompletion.create(...)
```

To a client-based interface:

```python
# New API
from openai import OpenAI

client = OpenAI()
response = client.chat.completions.create(...)
```

A migration like this requires more than updating a version number. Developers must understand release documentation, locate affected code, rewrite API usage, resolve compatibility problems, and test the resulting application.

**ASTon automates this workflow**, connecting documentation-driven change detection, AST-based repository analysis, LLM-assisted migration, isolated verification, and GitHub delivery.

---

## Quick start

ASTon works directly inside your target Git repository.

### 1. Install ASTon

```bash
pip install astoncli
```

### 2. Navigate to your project

```bash
cd your-project
```

### 3. Initialize the ASTon workspace

```bash
aston init
```

This initializes ASTon's repository-local workspace and configures `.gitignore` protection for its internal artifacts.

### 4. Run a migration

```bash
aston check --run
```

ASTon analyzes dependencies and executes the migration workflow.

### 5. Inspect the results

```bash
aston status
aston diff
aston report
```

Review staged migration files and the generated migration report before delivery.

### 6. Create a pull request

```bash
aston pr
```

ASTon uses the current repository and its default staging directory unless you provide explicit paths.

### Requirements

- Python
- Git
- Docker installed and running (unless verification is disabled)
- An API key for your configured LLM provider
- GitHub credentials and repository access for pull request creation

---

## Demo

ASTon has been demonstrated on a Python application using the legacy OpenAI Python SDK.

It analyzed the repository, identified affected API usage across multiple modules, generated migration changes, verified the updated code, and produced a GitHub pull request that was subsequently merged.

### Migration details

| | |
|---|---|
| Source dependency | `openai==0.28.1` |
| Target dependency | `openai==3.41.0` |
| Migration scope | Multiple Python modules |
| Verification | Docker-based verification and repository-level checks |

### Migration evidence

| Artifact | Reference |
|---|---|
| Original legacy code | [Initial commit](https://github.com/2022n02511-stack/demo_aston/commit/10cce48d300d828c774e56ed2e72066c2e993b33) |
| ASTon-generated changes | [Migration commit](https://github.com/2022n02511-stack/demo_aston/commit/66547808b4b4868a6684718c2d095928a5b6605a) |
| Merged migration | [Merge commit](https://github.com/2022n02511-stack/demo_aston/commit/caaea1080b9c7e603c6917fa0fd16c77ee93c8a2) |
| Before-and-after comparison | [View complete diff](https://github.com/2022n02511-stack/demo_aston/compare/10cce48d300d828c774e56ed2e72066c2e993b33...caaea1080b9c7e603c6917fa0fd16c77ee93c8a2) |
| ASTon source | [GitHub repository](https://github.com/2022n02511-stack/ASTon) |

The demo demonstrates breaking-change discovery, rule extraction, repository analysis, code migration, verification, and pull request delivery.

It is a demonstration project, not a production customer deployment.

---

## Repository-native workflow

ASTon uses a repository-local workspace rather than requiring developers to manage migration artifacts across separate directories and commands.

```text
your-project/
│
├── .git/
├── .aston/
│   ├── config.toml
│   ├── staging/
│   ├── runs/
│   └── debug/
│
├── src/
├── tests/
├── requirements.txt
└── ...
```

The `.aston/` workspace keeps migration state, staged changes, execution artifacts, and diagnostics associated with the repository.

### Familiar Git-style operations

| Command | Purpose |
|---|---|
| `aston init` | Initialize the ASTon workspace |
| `aston status` | Inspect staged migration files |
| `aston diff` | Compare working-tree files against staged migrations |
| `aston clean` | Clear staging and temporary run artifacts |
| `aston report` | Inspect migration results |
| `aston notifications` | View workspace notification history |

ASTon also provides commands for dependency analysis, configuration, diagnostics, and pull request delivery.

---

## How it works

ASTon organizes dependency migration into ten stages.

```text
  WATCH
    │
    ▼
  INGEST
    │
    ▼
  EXTRACT
    │
    ▼
  SCAN
    │
    ▼
  RELEVANCE
    │
    ▼
  VALIDATE
    │
    ▼
  GENERATE
    │
    ▼
  VERIFY ────────┐
    │            │
    │ failure    ▼
    │          REPAIR
    │            │
    └────────────┘
    │
    ▼
  FULL-REPOSITORY VERIFY
    │
    ▼
  STAGE MIGRATIONS
    │
    ▼
  REVIEW AND DELIVER
```

### 1. Dependency monitoring

ASTon identifies available dependency updates and maintains version-related state.

```bash
aston watcher
```

### 2. Documentation ingestion

ASTon collects migration information from:

- Release notes
- Changelogs
- Migration guides
- Relevant documentation pages
- Breaking-change discussions

### 3. Migration rule extraction

An LLM converts documentation into structured migration rules describing API changes, removals, configuration changes, and new instantiation requirements.

Example:

```json
{
  "package": "openai",
  "old_api": "openai.ChatCompletion.create",
  "new_api": "client.chat.completions.create",
  "migration_type": "rename"
}
```

### 4. Repository analysis

ASTon uses Python's Abstract Syntax Tree to inspect imports, aliases, instantiated objects, API calls, source locations, and relevant arguments.

This produces a repository usage map that connects dependency APIs to actual project code.

### 5. Relevance analysis

Extracted migration rules are compared with the repository usage map. Changes that do not affect the project's actual API usage can be excluded from the migration scope.

### 6. Target-version validation

Rules are evaluated against the requested target version.

ASTon separates documentation validation from runtime API probing. It groups validation by package and target version, and conservatively treats instance-bound API paths as unverifiable when the necessary client context is unavailable.

Unconfirmed rules are not silently treated as validated.

### 7. Migration generation

Relevant migration rules are used to generate candidate changes.

For example:

```python
# Before
import openai

response = openai.ChatCompletion.create(...)
```

```python
# After
from openai import OpenAI

client = OpenAI()
response = client.chat.completions.create(...)
```

### 8. Per-file verification and repair

Candidate files are checked in isolated Docker environments with the target dependencies.

ASTon distinguishes migration errors from environment, external-service, and timeout failures. Code-level failures can enter a repair loop using verification feedback.

### 9. Full-repository verification

Successfully migrated files are combined with the remaining repository files and updated requirements in a separate verification copy.

ASTon performs Python syntax checks and executes the configured repository test command.

A per-file pass does not guarantee full-repository compatibility. A repository with no discoverable tests is also distinguished from one whose test suite passed.

### 10. Staging and delivery

Successful migration output is staged inside the repository's `.aston/` workspace.

Developers can inspect the result with:

```bash
aston status
aston diff
aston report
```

Once satisfied, they can create a pull request:

```bash
aston pr
```

The original project remains under developer control, and the proposed changes can be reviewed before merging.

---

## CLI reference

ASTon's CLI is organized around repository initialization, workspace inspection, migration execution, and delivery.

### Workspace commands

**Initialize a repository**

```bash
aston init
aston init --path /path/to/project
```

**Inspect staged migrations**

```bash
aston status
```

**Review staged changes**

```bash
aston diff
```

**Clean migration artifacts**

```bash
aston clean
```

**Show configuration**

```bash
aston config show
```

**Run system diagnostics**

```bash
aston doctor
aston doctor --test-llm
```

### Migration commands

**Analyze and run migration**

```bash
aston check --run
```

**Force a new migration run**

```bash
aston check --run --force
```

**Override the LLM provider and model**

```bash
aston check --run \
  --provider mistral \
  --model codestral-latest
```

**Configure repair and sweep attempts**

```bash
aston check --run \
  --repair-retries 3 \
  --sweep-passes 2
```

**Configure verification**

```bash
aston check --run \
  --file-timeout 300 \
  --repo-timeout 600 \
  --build-timeout 600
```

**Override the verification environment**

```bash
aston check --run \
  --python-image python:3.11-slim \
  --network-mode auto \
  --requirements-file requirements.txt \
  --test-command "python -m pytest -q"
```

**Disable Docker verification for a run**

```bash
aston check --run --no-verify
```

Use this option only when you explicitly accept that the migration will not receive Docker-based verification.

### Monitoring and reporting

**Start dependency monitoring**

```bash
aston watcher
```

**View notification history**

```bash
aston notifications
```

**Display the latest migration report**

```bash
aston report
```

**Display a particular run**

```bash
aston report --run-id RUN_ID
```

**Export a report as JSON**

```bash
aston report --json
```

### Pull request delivery

```bash
aston pr
```

Specify a custom repository, staging directory, branch, or base branch when needed:

```bash
aston pr \
  --path .aston/staging \
  --repo /path/to/project \
  --branch chore/dependency-migration \
  --base main \
  --title "chore: migrate dependency" \
  --body "Dependency migration generated by ASTon."
```

Create a draft pull request:

```bash
aston pr --draft
```

For the complete list of options:

```bash
aston --help
aston check --help
aston pr --help
```

---

## Configuration and diagnostics

ASTon supports configuration for the LLM provider, migration behavior, and Docker verification.

The `config` and `doctor` commands provide ways to inspect configuration and diagnose the local environment.

```bash
aston config show
aston doctor
aston doctor --test-llm
```

The `check` command also supports per-run overrides for the provider, model, retry limits, verification timeouts, Python image, network mode, requirements file, and test command.

For example:

```bash
aston check --run \
  --provider mistral \
  --model codestral-latest \
  --file-timeout 300 \
  --repo-timeout 600 \
  --network-mode auto
```

---

## Architecture

ASTon's implementation separates repository analysis, migration intelligence, verification, and delivery.

| Component | Responsibility |
|---|---|
| `poller.py` | Dependency monitoring |
| `ingestion.py` | Release and migration documentation |
| `changes/` | Change discovery, extraction, and version boundaries |
| `axios.py` | LLM-based extraction and migration generation |
| `scanner/` | AST-based source analysis |
| `relevance/` | Migration-rule relevance |
| `rule_validation.py` | Target-version API validation |
| `pipeline.py` | Migration orchestration |
| `docker.py` | Per-file verification and repair |
| `repo_verification.py` | Full-repository verification |
| `repo_index/` | Repository mapping and migration coordination |
| `github_integrations/` | GitHub pull request integration |
| `cli/` | Command-line interface and terminal output |
| `config.py` | Runtime and repository configuration |
| `ledger.py` | Local notification and execution history |

### Generated artifacts

| Location | Purpose |
|---|---|
| `.aston/staging/` | Staged migration output |
| `.aston/runs/` | Per-run execution artifacts |
| `.aston/debug/` | Diagnostic logs and failure details |
| `.aston/` configuration | Repository-specific ASTon settings |

---

## Design principles

| Principle | Implementation |
|---|---|
| Repository-native workflow | Keep migration state and artifacts with the target repository |
| Documentation-driven migration | Ground migration rules in release and migration documentation |
| Repository-aware changes | Focus on APIs actually used by the project |
| Version-aware validation | Evaluate changes against the requested target version |
| Verification before acceptance | Treat generated code as a candidate, not a trusted result |
| Isolated execution | Use Docker for dependency installation and verification |
| Reviewable delivery | Keep proposed changes inspectable before pull request merging |

---

## Roadmap

ASTon is under active development, with its current implementation focused on Python dependency migrations. The OpenAI Python SDK is the primary end-to-end demonstration.

### Implemented

- [x] Repository-local workspace initialization
- [x] Git-style status, diff, and clean operations
- [x] Dependency monitoring
- [x] Documentation ingestion and rule extraction
- [x] AST-based repository usage mapping
- [x] Migration-rule relevance analysis
- [x] Target-version API validation
- [x] Docker-based per-file verification and repair
- [x] Full-repository verification workflow
- [x] Migration reports and local notifications
- [x] GitHub pull request creation
- [x] PyPI distribution

### In progress and planned

- [ ] Broader validation across dependency version chains
- [ ] Reliability testing across more real-world repositories
- [ ] Support for additional Python packages
- [ ] Broader language ecosystem support

**Current scope:** ASTon has completed an end-to-end migration demonstration on a project using the OpenAI Python SDK. Reliability across different repository structures, dependency configurations, and test environments is still being developed and evaluated.

---

## Troubleshooting

### ASTon cannot find a repository

Run commands from inside the target repository or specify its path:

```bash
aston init --path /path/to/project
```

### An existing migration is detected

ASTon prevents accidentally repeating a completed migration.

Inspect the workspace first:

```bash
aston status
aston report
```

If you intentionally want to rerun migration:

```bash
aston check --run --force
```

### Docker verification fails

Check Docker availability, dependency installation, network connectivity, test configuration, and timeout settings.

Inspect:

```text
.aston/debug/
```

Use the verification options on `aston check` to adjust the environment for the target project.

### How do I review migration output?

```bash
aston status
aston diff
aston report
```

### How do I clear migration artifacts?

```bash
aston clean
```

Review the workspace before cleaning, since this command clears the staging area and temporary run artifacts.

---

## Contributing

Contributions, feedback, and bug reports are welcome.

When reporting an issue, include:

- Dependency name and source version
- Requested target version
- Relevant source code
- Migration report
- Verification output and relevant `.aston/debug/` logs

These details make migration issues easier to reproduce and investigate.

---

**ASTon — Making dependency migrations less manual.**

If ASTon is useful to you, consider starring the [GitHub repository](https://github.com/2022n02511-stack/ASTon).