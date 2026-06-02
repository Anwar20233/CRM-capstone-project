import { themeCssVariables } from 'twenty-ui/theme-constants';

type TagColor = keyof typeof themeCssVariables.tag.background;

// Consistent color per entity type. Keyed by the raw NER label first (finer
// grained), falling back to a neutral gray for anything unmapped.
const TAG_COLOR_BY_LABEL: Record<string, TagColor> = {
  person: 'blue',
  company: 'green',
  competitor: 'red',
  deal: 'purple',
  money: 'amber',
  'email address': 'sky',
  'phone number': 'turquoise',
  location: 'jade',
  date: 'sand',
  product: 'orange',
  'job title': 'mauve',
};

const DEFAULT_TAG_COLOR: TagColor = 'gray';

export const getEntityHighlightColor = (
  label: string,
): { background: string; text: string } => {
  const color = TAG_COLOR_BY_LABEL[label] ?? DEFAULT_TAG_COLOR;

  return {
    background: themeCssVariables.tag.background[color],
    text: themeCssVariables.tag.text[color],
  };
};
