import { REACT_APP_AI_SERVICE_URL } from '@/followup-intelligence/utils/get-ai-service-base-url';

// The agents' real source files on disk (emailer + next_step), browsable and
// editable from the Skills UI. These are NOT the DB-backed skills — saving here
// overwrites the actual file the running service reads.
export type AgentFile = {
  // Opaque handle used to read/save — not shown in the UI.
  path: string;
  agent: string;
  folder: string;
  title: string;
  category: string;
  preview: string;
};

export type AgentFileContent = {
  path: string;
  content: string;
};

const agentFilesRequest = async <TResponse>(
  path: string,
  options?: RequestInit,
): Promise<TResponse> => {
  const response = await fetch(`${REACT_APP_AI_SERVICE_URL}${path}`, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...options?.headers,
    },
  });

  if (!response.ok) {
    const detail = await response.text();
    throw new Error(
      detail || `Agent files request failed with status ${response.status}`,
    );
  }

  return response.json() as Promise<TResponse>;
};

export const listAgentFiles = async (): Promise<AgentFile[]> =>
  agentFilesRequest<AgentFile[]>('/followup/agent-files');

export const fetchAgentFileContent = async (
  path: string,
): Promise<AgentFileContent> =>
  agentFilesRequest<AgentFileContent>(
    `/followup/agent-files/content?path=${encodeURIComponent(path)}`,
  );

export const saveAgentFileContent = async (
  path: string,
  content: string,
): Promise<AgentFileContent> =>
  agentFilesRequest<AgentFileContent>('/followup/agent-files/content', {
    method: 'PUT',
    body: JSON.stringify({ path, content }),
  });
