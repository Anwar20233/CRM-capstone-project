# Twenty Agent Tools Reference

Generated reference for the agentic tool system under
`packages/twenty-server/src/engine/core-modules/tool-provider/`.

Tools are **not** a static list. They are produced at runtime by 8 _providers_,
filtered by the calling role's permissions, and surfaced to the model through a
4-step meta-tool flow. This file documents the complete inventory and how it is
assembled.

---

## Meta-tools (the only tools the model sees directly)

Defined in `tool-provider/tools/`. The model uses these to discover and run the
real tools — it never gets the full tool set injected up front.

| Tool               | Step | Purpose                                                                                           |
| :----------------- | :--- | :------------------------------------------------------------------------------------------------ |
| `get_tool_catalog` | 1    | Browse real tools by category. Returns `{ name, description }[]` grouped by category. No schemas. |
| `learn_tools`      | 2    | Fetch input schemas (and/or descriptions) for specific tool names.                                |
| `execute_tool`     | 3    | Run one tool by exact name with arguments.                                                        |
| `load_skill`       | —    | Load a skill (prompt bundle) into context.                                                        |

`search_help_center` is always preloaded (`constants/common-preload-tools.const.ts`).

---

## Categories (`twenty-shared/src/ai/constants/tool-category.const.ts`)

```
DATABASE_CRUD · ACTION · WORKFLOW · METADATA · VIEW · VIEW_FIELD · DASHBOARD · LOGIC_FUNCTION
```

Each maps to one provider registered under the `TOOL_PROVIDERS` token in
`tool-provider.module.ts`.

---

## 1. DATABASE_CRUD — `providers/database-tool.provider.ts`

Generated **per object** (e.g. `person`, `company`, `opportunity`), gated by the
role's object-level permissions. `<singular>`/`<plural>` are the object's
snake_cased names.

| Tool                   | Permission gate        | Operation                                       |
| :--------------------- | :--------------------- | :---------------------------------------------- |
| `find_<plural>`        | canRead                | filter/sort/paginate, returns records           |
| `find_one_<singular>`  | canRead                | fetch one by id                                 |
| `group_by_<plural>`    | canRead (if groupable) | aggregate (COUNT/SUM/AVG/MIN/MAX) by 1–2 fields |
| `create_<singular>`    | canUpdate              | create one                                      |
| `create_many_<plural>` | canUpdate              | create up to 20                                 |
| `update_<singular>`    | canUpdate              | partial update by id                            |
| `update_many_<plural>` | canUpdate              | bulk update by filter                           |
| `delete_<singular>`    | canSoftDelete          | soft-delete by id                               |

Execution: `executionRef.kind = 'database_crud'` → routed in
`tool-executor.service.ts` to the `record-crud` services.

## 2. ACTION — `providers/action-tool.provider.ts`

Fixed set, each gated by a `PermissionFlagType`.

| Tool                 | Gate                |
| :------------------- | :------------------ |
| `http_request`       | `HTTP_REQUEST_TOOL` |
| `send_email`         | `SEND_EMAIL_TOOL`   |
| `draft_email`        | `SEND_EMAIL_TOOL`   |
| `search_help_center` | always available    |
| `code_interpreter`   | (see provider)      |
| `navigate_app`       | (see provider)      |

## 3. METADATA — `providers/metadata-tool.provider.ts`

Schema editing. Tools from `object-metadata` + `field-metadata` factories.

- `get_object_metadata`, `create_object_metadata`, `create_many_object_metadata`,
  `update_object_metadata`, `update_many_object_metadata`, `delete_object_metadata`
- `get_field_metadata`, `create_field_metadata`, `create_many_field_metadata`,
  `create_many_relation_fields`, `update_field_metadata`,
  `update_many_field_metadata`, `delete_field_metadata`

## 4. VIEW — `providers/view-tool.provider.ts`

Tools from `view` + `view-filter` + `view-sort` factories.

- Views: `get_views`, `create_view`, `update_view`, `delete_view`,
  `get_view_query_parameters`
- Filters: `get_view_filters`, `create_view_filter`, `create_many_view_filters`,
  `update_view_filter`, `delete_view_filter`
- Sorts: `get_view_sorts`, `create_view_sort`, `create_many_view_sorts`,
  `update_view_sort`, `delete_view_sort`

## 5. VIEW_FIELD — `providers/view-field-tool.provider.ts`

- `get_view_fields`, `create_view_field`, `create_many_view_fields`,
  `update_view_field`, `update_many_view_fields`, `delete_view_field`

## 6. WORKFLOW — `providers/workflow-tool.provider.ts`

Optional provider; available only when `WorkflowToolsModule` (a `@Global` module)
is loaded, which binds `WORKFLOW_TOOL_SERVICE_TOKEN`. Tools in
`modules/workflow/workflow-tools/tools/`.

- `create_complete_workflow` — trigger + steps + edges in one call
- `get_workflow_current_version`
- `create_draft_from_workflow_version`
- `activate_workflow_version`, `deactivate_workflow_version`
- `create_workflow_version_step`, `update_workflow_version_step`, `delete_workflow_version_step`
- `create_workflow_version_edge`, `delete_workflow_version_edge`
- `update_workflow_version_trigger`
- `update_workflow_version_positions`
- `compute_step_output_schema`
- `list_logic_function_tools`, `update_logic_function_source`

## 7. DASHBOARD — `providers/dashboard-tool.provider.ts`

Optional provider; bound via `DASHBOARD_TOOL_SERVICE_TOKEN` from
`modules/dashboard/tools/dashboard-tools.module.ts`.

- `list_dashboards`, `get_dashboard`
- `create_complete_dashboard`
- `add_dashboard_tab`
- `add_dashboard_widget`, `update_dashboard_widget`, `delete_dashboard_widget`

## 8. LOGIC_FUNCTION — `providers/logic-function-tool.provider.ts`

Generated **per workspace serverless function**. One tool per logic function
that has an input schema; tool name derived from the function name, description
from the function's own description. Execution:
`executionRef.kind = 'logic_function'` → `logicFunctionExecutorService.execute`.

---

## How `get_tool_catalog` assembles the catalog

Call chain:

```
get_tool_catalog.execute(parameters)
  └─ ToolRegistryService.buildToolIndex(workspaceId, roleId, options)
       └─ buildContextFromToolContext(...)        // role → rolePermissionConfig { unionOf: [roleId] }
       └─ getCatalog(context)
            └─ Promise.all(providers.map(provider =>
                 provider.isAvailable(context)
                   ? provider.generateDescriptors(context, { includeSchemas: false })
                   : []
               )).flat()
  └─ filter by parameters.categories (optional)
  └─ filter by options.excludeTools (optional)
  └─ group entries into catalog[category] = [{ name, description }]
```

Key points:

- **Providers run in parallel** (`Promise.all`) since they're independent.
- **`includeSchemas: false`** for the catalog — only name + description, keeping
  the payload light. Schemas are generated on demand later.
- **Permission filtering happens inside each provider** via `isAvailable` and
  per-tool permission checks, using the role-derived `rolePermissionConfig`.
- The role context is built from a single `roleId` as `{ unionOf: [roleId] }`;
  the executor also supports `intersectionOf` for multi-role configs.

### Steps 2 & 3 (for completeness)

- `learn_tools` → `ToolRegistryService.getToolInfo(names, ...)` → `resolveSchemas`
  re-runs the matching provider(s) with `includeSchemas: true` and returns the
  JSON Schemas for just the requested names.
- `execute_tool` → `ToolRegistryService.resolveAndExecute(name, args, ...)` →
  finds the catalog entry → `ToolExecutorService.dispatch(descriptor, args, ctx)`
  which switches on `descriptor.executionRef.kind`:
  - `database_crud` → record-crud services
  - `static` → owning provider's `executeStaticTool(toolId, args, ctx)`
    (re-checks `isAvailable` as defense-in-depth)
  - `logic_function` → logic function executor

### Eager paths (MCP / workflow agents)

`getToolsByCategories(context, { categories, ... })` skips the 3-step dance and
hydrates a full AI-SDK `ToolSet` (with schemas + dispatch closures) for whole
categories at once. Used where the model needs tools preloaded rather than
discovered.

---

## Extending the system

1. Implement the `ToolProvider` interface
   (`interfaces/tool-provider.interface.ts`): `category`, `isAvailable`,
   `generateDescriptors`, `executeStaticTool`.
2. Emit `ToolIndexEntry` (no schema) when `includeSchemas: false`, full
   `ToolDescriptor` (with `inputSchema`) otherwise.
3. Register the provider in `tool-provider.module.ts` under the `TOOL_PROVIDERS`
   factory.
4. For static tools, set `executionRef.kind = 'static'` and handle dispatch in
   your provider's `executeStaticTool`. For DB-style or function-style tools,
   reuse the `database_crud` / `logic_function` execution refs.
