# Mozart Unified Dashboard

A single web UI consolidating all Mozart & TestRail tools:

| Tool | Description |
|------|-------------|
| **Milestone Audit** | Compare two TestRail milestones for Pass↔Fail deviations |
| **Failure Analyzer** | Upload ZIP → get failure root causes & categorized reports |
| **TestRail Mapping** | Copy passed results from parent run to child run |
| **Mozart Skip Tool** | Bulk-skip passed cases in Mozart suites after scheduler runs |
| **Auto-Rebase** | Rebase all workspace projects onto mainline with one click |

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Configure environment (see Configuration below)
cp .env.example .env
# edit .env with your values, then export them:
export $(grep -v '^#' .env | xargs)

# Run the dashboard
python3 app.py
```

Open **http://localhost:**** in your browser.

## Configuration

All environment-specific values are read from environment variables. No
credentials or internal endpoints are hardcoded. Copy `.env.example` to `.env`
and fill in your values:

| Variable | Description |
|----------|-------------|
| `TESTRAIL_URL` | Base URL of your TestRail instance |
| `TESTRAIL_USERNAME` | TestRail account username/email |
| `TESTRAIL_API_KEY` | TestRail API key |
| `WORKSPACE_PATH` | Path to your local git workspace (default: `~/mozart-workspace`) |
| `FAILURE_ANALYZER_PATH` | Path to the failure-analyzer module (default: `~/mozart-failure-analyzer`) |
| `SKIP_TOOL_PATH` | Path to the skip-tool module (default: `~/mozart-skip-tool-web`) |
| `CODE_REVIEW_HOST` | Base URL of your code review host (for CR Analyzer) |
| `CODE_REVIEW_COOKIE` | Path to your code review auth cookie (default: `~/.review/cookie`) |
| `DEFAULT_GIT_REMOTE` | Default git remote for Auto-Rebase (default: `origin`) |

## Requirements

- Python 3.9+
- Network access to TestRail (for Milestone Audit, Mapping tools)
- Failure Analyzer module (path set via `FAILURE_ANALYZER_PATH`)
- Skip Tool module (path set via `SKIP_TOOL_PATH`)
- A local git workspace (path set via `WORKSPACE_PATH`, for Auto-Rebase)

## Tools Overview

### 1. Milestone Audit Tool
- Enter project ID and two milestone IDs
- Finds common test cases with status deviations
- Export results to CSV

### 2. Failure Analyzer
- Drag & drop a results ZIP file
- Parses logs and identifies root causes
- Shows categorized failure table

### 3. TestRail Mapping Tool
- Enter parent and child run IDs
- Copies all passed results from parent → child
- Shows count of copied results

### 4. Skip Tool
- Upload the scheduler JSON payload
- Enter target suite name
- Preview and apply skip changes

### 5. Auto-Rebase Tool
- Shows status of all workspace git repos
- Select projects to rebase
- One-click rebase onto the configured remote/mainline
