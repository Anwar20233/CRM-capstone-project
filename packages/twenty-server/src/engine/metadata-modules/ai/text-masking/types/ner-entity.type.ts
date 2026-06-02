// An entity as returned by the AI service's /ner/extract route.
export type NerEntity = {
  label: string; // person | company | deal | money | date | email address | ...
  text: string;
  score: number;
  start: number | null;
  end: number | null;
};
