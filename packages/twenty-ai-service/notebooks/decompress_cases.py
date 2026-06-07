import json
import re
import base64
import zlib

notebook_path = '/home/ibrahim/SDA/Project/packages/twenty-ai-service/notebooks/CRM_NER_Pipeline_v3.ipynb'
output_path = '/home/ibrahim/SDA/Project/packages/twenty-ai-service/notebooks/cases_data.json'

with open(notebook_path, 'r', encoding='utf-8') as f:
    nb = json.load(f)

cases_data_str = None
for cell in nb.get('cells', []):
    if cell.get('cell_type') == 'code':
        source = ''.join(cell.get('source', []))
        if '_CASES_DATA =' in source:
            m = re.search(r'_CASES_DATA\s*=\s*["\'](.*?)["\']', source, re.DOTALL)
            if m:
                cases_data_str = m.group(1).replace('\n', '').replace('\\', '').strip()
                break

if cases_data_str:
    try:
        decoded_bytes = base64.b64decode(cases_data_str)
        decompressed_text = zlib.decompress(decoded_bytes).decode('utf-8')
        json_data = json.loads(decompressed_text)
        with open(output_path, 'w', encoding='utf-8') as out_f:
            json.dump(json_data, out_f, indent=2, ensure_ascii=False)
        print(f"Successfully wrote decompressed data ({len(json_data)} cases) to: {output_path}")
    except Exception as e:
        print(f"Error during decoding/decompression: {e}")
else:
    print("_CASES_DATA not found in the notebook.")
