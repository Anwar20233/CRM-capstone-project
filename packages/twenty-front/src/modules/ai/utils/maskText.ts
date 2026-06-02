import { type MaskedEntity } from 'twenty-shared/ai';

import { REST_API_BASE_URL } from '@/apollo/constant/rest-api-base-url';

export type MaskTextResult = {
  maskedText: string;
  sessionId: string;
  entities: MaskedEntity[];
};

// Calls the text-masking endpoint (NER + masking). Used by the AI chat to
// annotate the user's message instead of sending it to an LLM.
export const maskText = async ({
  text,
  token,
  sessionId,
}: {
  text: string;
  token: string;
  sessionId?: string;
}): Promise<MaskTextResult> => {
  const response = await fetch(`${REST_API_BASE_URL}/text-masking/mask`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify({ text, sessionId }),
  });

  if (!response.ok) {
    throw new Error(`Text masking failed with status ${response.status}`);
  }

  return response.json();
};
