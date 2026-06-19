import { canObjectBeManagedByWorkflow } from '@/workflow/utils/canObjectBeManagedByWorkflow';

describe('canObjectBeManagedByWorkflow', () => {
  it('should return true for non-system, non-excluded objects', () => {
    expect(
      canObjectBeManagedByWorkflow({
        nameSingular: 'company',
        isSystem: false,
      }),
    ).toBe(true);
  });

  it('should return false for system objects', () => {
    expect(
      canObjectBeManagedByWorkflow({
        nameSingular: 'company',
        isSystem: true,
      }),
    ).toBe(false);
  });

  it('should return false for workflow object', () => {
    expect(
      canObjectBeManagedByWorkflow({
        nameSingular: 'workflow',
        isSystem: false,
      }),
    ).toBe(false);
  });

  it('should return false for workflowVersion object', () => {
    expect(
      canObjectBeManagedByWorkflow({
        nameSingular: 'workflowVersion',
        isSystem: false,
      }),
    ).toBe(false);
  });

  it('should return false for workflowRun object', () => {
    expect(
      canObjectBeManagedByWorkflow({
        nameSingular: 'workflowRun',
        isSystem: false,
      }),
    ).toBe(false);
  });

  it('should return false for dashboard object', () => {
    expect(
      canObjectBeManagedByWorkflow({
        nameSingular: 'dashboard',
        isSystem: false,
      }),
    ).toBe(false);
  });

  it('should return true for noteTarget even though it is a system object', () => {
    expect(
      canObjectBeManagedByWorkflow({
        nameSingular: 'noteTarget',
        isSystem: true,
      }),
    ).toBe(true);
  });

  it('should return true for taskTarget even though it is a system object', () => {
    expect(
      canObjectBeManagedByWorkflow({
        nameSingular: 'taskTarget',
        isSystem: true,
      }),
    ).toBe(true);
  });
});
