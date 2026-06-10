import os
import sys
import json
import httpx
from dotenv import load_dotenv

# Ensure we can import modules from packages/twenty-ai-service
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agent.schema_compactor import compact_schema

def main():
    # Load env variables from packages/twenty-ai-service/.env
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    if os.path.exists(env_path):
        load_dotenv(env_path)
    else:
        load_dotenv()
    
    workspace_id = os.environ.get("TWENTY_WORKSPACE_ID")
    role_id = os.environ.get("TWENTY_ROLE_ID")
    node_bridge_url = os.environ.get("NODE_BRIDGE_BASE_URL", "http://localhost:3000/agent-bridge")
    
    if not workspace_id or not role_id:
        print("Error: TWENTY_WORKSPACE_ID and TWENTY_ROLE_ID must be set in .env")
        sys.exit(1)
        
    print(f"Connecting to Node bridge at: {node_bridge_url}")
    print(f"Workspace ID: {workspace_id}")
    print(f"Role ID: {role_id}")
    
    # 1. Fetch catalog
    print("Fetching tool catalog...")
    catalog_url = f"{node_bridge_url}/catalog"
    try:
        res = httpx.post(catalog_url, json={
            "workspaceId": workspace_id,
            "roleId": role_id
        }, timeout=60.0)
        res.raise_for_status()
        catalog_payload = res.json()
    except Exception as e:
        print(f"Failed to fetch catalog: {e}")
        sys.exit(1)
        
    if not catalog_payload.get("ok"):
        print(f"Catalog response error: {catalog_payload.get('error')}")
        sys.exit(1)
        
    catalog = catalog_payload.get("data", {}).get("catalog", {})
    if not catalog:
        print("No catalog found in response.")
        sys.exit(1)
        
    # Setup output directory in the project root
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(script_dir)))
    output_dir = os.path.join(project_root, "tools_metadata")
    
    print(f"Creating output folders under: {output_dir}")
    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(os.path.join(output_dir, "categories"), exist_ok=True)
    os.makedirs(os.path.join(output_dir, "tools"), exist_ok=True)
    
    # Save the full catalog
    with open(os.path.join(output_dir, "catalog.json"), "w") as f:
        json.dump(catalog, f, indent=2)
    print("Saved tools_metadata/catalog.json")
    
    # Collect all tools to learn
    all_tools = []
    tool_to_category = {}
    
    for category, tools_list in catalog.items():
        print(f"Category {category}: {len(tools_list)} tools found.")
        # Save individual category files
        with open(os.path.join(output_dir, "categories", f"{category}.json"), "w") as f:
            json.dump(tools_list, f, indent=2)
            
        os.makedirs(os.path.join(output_dir, "tools", category), exist_ok=True)
        for t in tools_list:
            tool_name = t["name"]
            all_tools.append(tool_name)
            tool_to_category[tool_name] = category
            
    print(f"Total tools to fetch details for: {len(all_tools)}")
    
    # Fetch tools schema in batches of 20
    batch_size = 20
    tool_schemas = {}
    
    for i in range(0, len(all_tools), batch_size):
        batch = all_tools[i:i+batch_size]
        print(f"Fetching details for batch {i//batch_size + 1}/{-(-len(all_tools)//batch_size)}: {len(batch)} tools...")
        learn_url = f"{node_bridge_url}/learn"
        try:
            res = httpx.post(learn_url, json={
                "toolNames": batch,
                "workspaceId": workspace_id,
                "roleId": role_id
            }, timeout=60.0)
            res.raise_for_status()
            learn_payload = res.json()
            if learn_payload.get("ok"):
                tools_data = learn_payload.get("data", {}).get("tools", [])
                for t_data in tools_data:
                    name = t_data["name"]
                    tool_schemas[name] = t_data
            else:
                print(f"Batch fetch error for tools {batch}: {learn_payload.get('error')}")
        except Exception as e:
            print(f"Failed to fetch batch {batch}: {e}")
            
    # Save each tool's raw and compact schema
    success_count = 0
    for name in all_tools:
        if name not in tool_schemas:
            print(f"Warning: No schema retrieved for tool '{name}'")
            continue
            
        category = tool_to_category[name]
        tool_data = tool_schemas[name]
        raw_schema = tool_data.get("inputSchema", {})
        
        # Save raw tool info
        tool_file_path = os.path.join(output_dir, "tools", category, f"{name}.json")
        with open(tool_file_path, "w") as f:
            json.dump(tool_data, f, indent=2)
            
        # Compact schema
        try:
            compact_inp_schema = compact_schema(raw_schema)
            compact_tool_data = {
                "name": tool_data.get("name"),
                "description": tool_data.get("description"),
                "inputSchema": compact_inp_schema
            }
            compact_file_path = os.path.join(output_dir, "tools", category, f"{name}.compact.json")
            with open(compact_file_path, "w") as f:
                json.dump(compact_tool_data, f, indent=2)
        except Exception as e:
            print(f"Warning: Failed to compact schema for '{name}': {e}")
            
        success_count += 1
        
    print(f"Successfully processed {success_count}/{len(all_tools)} tools.")
    print(f"Done! Folder structure created at: {output_dir}")

if __name__ == "__main__":
    main()
