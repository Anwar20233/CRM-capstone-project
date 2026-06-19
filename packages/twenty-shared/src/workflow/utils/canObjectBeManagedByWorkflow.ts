// System objects are normally off-limits to workflows/agents, but a few are
// user-meaningful join records that creating notes/tasks against records
// legitimately needs. Without these, attaching a note or task to a
// person/company/opportunity is impossible through the workflow/agent write path
// (the join object is system, so the record-crud create is otherwise rejected).
const allowedSystemObjectMetadataItemNames = ['noteTarget', 'taskTarget'];

export const canObjectBeManagedByWorkflow = ({
  nameSingular,
  isSystem,
}: {
  nameSingular: string;
  isSystem: boolean;
}) => {
  const excludedNonSystemObjectMetadataItemNames = [
    'workflow',
    'workflowVersion',
    'workflowRun',
    'dashboard',
  ];

  if (excludedNonSystemObjectMetadataItemNames.includes(nameSingular)) {
    return false;
  }

  if (allowedSystemObjectMetadataItemNames.includes(nameSingular)) {
    return true;
  }

  return !isSystem;
};
