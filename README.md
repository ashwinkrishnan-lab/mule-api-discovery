# MuleSoft API Discovery Tool

Discover and catalog all APIs in your MuleSoft Anypoint Platform organization.

## What It Does

Connects to your Anypoint Platform and gathers:

- **Exchange Assets** - API specifications (RAML/OAS), documentation, categories
- **API Manager** - Deployed API instances, policies
- **Runtime Manager** - Applications, vCore usage, deployment status
- **Visualizer** - Application dependency graph (who calls whom)
- **Organizations** - Business groups and environments

## Requirements

- Python 3.8+
- MuleSoft Anypoint Platform account
- Connected App with appropriate scopes

## Installation

```bash
# Clone the repository
git clone https://github.com/YOUR_USERNAME/mule-api-discovery.git
cd mule-api-discovery

# Install dependencies
pip install -r requirements.txt
```

## Setup: Create a Connected App

1. Log into [Anypoint Platform](https://anypoint.mulesoft.com)
2. Navigate to **Access Management** → **Connected Apps**
3. Click **Create App**
4. Configure:
   - **Name**: API Discovery Tool
   - **Type**: App acts on its own behalf (client credentials)
5. Add these scopes:
   - `General` → View Organization
   - `Exchange` → Exchange Viewer
   - `Runtime Manager` → Read Applications
   - `API Manager` → View APIs Configuration
   - `Visualizer` → Visualizer Viewer (optional)
6. Save and copy:
   - **Client ID**
   - **Client Secret**
7. Find your **Organization ID**:
   - Go to **Access Management** → **Organization**
   - Copy the ID from the URL or details panel

## Usage

### Basic Discovery

```bash
python mule_api_discovery.py \
    --client-id "YOUR_CLIENT_ID" \
    --client-secret "YOUR_CLIENT_SECRET" \
    --org-id "YOUR_ORG_ID"
```

### EU Region

```bash
python mule_api_discovery.py \
    --client-id "xxx" \
    --client-secret "xxx" \
    --org-id "xxx" \
    --region eu
```

### Quick Discovery (Skip Specifications)

```bash
python mule_api_discovery.py \
    --client-id "xxx" \
    --client-secret "xxx" \
    --org-id "xxx" \
    --no-specs --no-docs
```

## Options

### Discovery Options

| Option | Description |
|--------|-------------|
| `--region` | Platform region: `us` (default), `eu`, `gov` |
| `--no-exchange` | Skip Exchange assets |
| `--no-specs` | Skip downloading API specifications |
| `--no-docs` | Skip documentation pages |
| `--no-visualizer` | Skip dependency graph |
| `--no-runtime` | Skip Runtime Manager applications |
| `--asset-types` | Filter to specific asset types (e.g., `rest-api`) |

### Rate Limiting (for large organizations)

| Option | Default | Description |
|--------|---------|-------------|
| `--rps` | 5 | Requests per second |
| `--batch-size` | 50 | Assets per batch before pause |
| `--batch-pause` | 5 | Seconds to pause between batches |
| `--max-retries` | 5 | Max retries on rate limit errors |

### Output

| Option | Description |
|--------|-------------|
| `--output-dir` | Output directory (default: `discovery_output`) |
| `--verbose` | Show detailed logs |

## Examples

```bash
# Full discovery with all data
python mule_api_discovery.py --client-id xxx --client-secret xxx --org-id xxx

# Large organization (500+ assets) - use slower rate
python mule_api_discovery.py --client-id xxx --client-secret xxx --org-id xxx --rps 2

# Very large organization - conservative settings
python mule_api_discovery.py --client-id xxx --client-secret xxx --org-id xxx \
    --rps 2 --batch-size 30 --batch-pause 10

# Only API assets (skip connectors, templates, etc.)
python mule_api_discovery.py --client-id xxx --client-secret xxx --org-id xxx \
    --asset-types rest-api http-api

# Quick runtime-only discovery
python mule_api_discovery.py --client-id xxx --client-secret xxx --org-id xxx \
    --no-exchange
```

## Output

The tool creates an output directory with:

```
discovery_output/
├── full_discovery_20241211_143052.json   # Complete data
├── summary_20241211_143052.json          # Summary statistics
└── api_specs/                             # Raw API specifications
    ├── my-api_1.0.0.txt
    └── ...
```

### Sample Output Structure

```json
{
  "discovery_timestamp": "2024-12-11T14:30:52.123456",
  "master_org": {
    "org_id": "abc123",
    "org_name": "My Company"
  },
  "organizations": [...],
  "environments": [...],
  "applications": [...],
  "exchange_assets": [...],
  "api_instances": [...],
  "sandbox_graph": { "nodes": [...], "edges": [...] },
  "production_graph": { "nodes": [...], "edges": [...] }
}
```

## Time Estimates

| Assets | Default (5 RPS) | Conservative (2 RPS) |
|--------|-----------------|----------------------|
| 100 | ~2 min | ~5 min |
| 500 | ~10 min | ~25 min |
| 1,000 | ~20 min | ~50 min |
| 2,000 | ~40 min | ~100 min |

## Troubleshooting

### "Failed to authenticate"
- Verify Client ID and Client Secret are correct
- Ensure the Connected App is enabled
- Check all required scopes are added

### Rate limiting (429 errors)
- Use `--rps 2` or lower
- Increase `--batch-pause`
- Run during off-peak hours

### Timeout errors
- Large Visualizer graphs may timeout
- Use `--no-visualizer` if problematic

### Permission denied
- Ensure Connected App has all required scopes
- Verify you have access to all business groups

## Security

- **Read-only**: This tool cannot modify anything in your platform
- **Credentials**: Used only during execution, not stored
- **Output**: All data stays on your local machine

## Data Collected

| Category | Data Points |
|----------|-------------|
| Organizations | ID, name, hierarchy |
| Environments | ID, name, type (sandbox/production) |
| Applications | Name, status, vCore, workers, region, Mule version |
| Exchange Assets | Specs, docs, categories, tags, dependencies |
| API Instances | Endpoint, status, policies applied |
| Dependencies | Visualizer graph (nodes and edges) |

## License

MIT License - See LICENSE file for details.

