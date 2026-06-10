# Twenty CRM Tool Catalog and Schema Export

This folder contains a complete export of the Twenty CRM tool catalog and detailed input schemas, fetched dynamically from the Node agent-bridge server.

The files are structured specifically to make them easy for LLMs and other agents to discover, learn, and use.

## Folder Structure

```
tools_metadata/
├── README.md                  # This file
├── catalog.json               # Full catalog listing all tools categorized, including names and descriptions
├── categories/                # Categorized tool listings
│   ├── ACTION.json
│   ├── DASHBOARD.json
│   ├── DATABASE_CRUD.json
│   ├── METADATA.json
│   ├── VIEW.json
│   ├── VIEW_FIELD.json
│   └── WORKFLOW.json
└── tools/                     # Detailed tool schemas grouped by category
    ├── ACTION/
    │   ├── code_interpreter.json          # Raw tool schema
    │   ├── code_interpreter.compact.json  # Compacted schema optimized for LLM token efficiency
    │   └── ...
    ├── DASHBOARD/
    ├── DATABASE_CRUD/
    ├── METADATA/
    ├── VIEW/
    ├── VIEW_FIELD/
    └── WORKFLOW/
```

## Schema Versions

For each tool, we generate two JSON files:
1. **`<tool_name>.json` (Raw Schema)**: Contains the exact raw schema returned by the Node bridge, including all validations, parameters, and patterns.
2. **`<tool_name>.compact.json` (Compacted Schema)**: A minimized schema processed through Twenty's `schema_compactor`. It strips out validator-only noise (e.g. JS max-safe-integer bounds, redundant filter operators, boilerplate descriptions) to save LLM context window tokens while preserving all semantic meaning.

## How to Regenerate

If the tools or objects change, you can regenerate this folder by running:
```bash
# From packages/twenty-ai-service
.venv/bin/python scripts/export_tools.py
```
