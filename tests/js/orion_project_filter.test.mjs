import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';

const source = await readFile(new URL('../../harness/web/assets/modules/engineering/project-lineage.js', import.meta.url), 'utf8');
function lineage() {
  const context = vm.createContext({ window: {} });
  vm.runInContext(source, context);
  return context.window.__ORION_PROJECT_LINEAGE__;
}
const row = (id, kind, extra = {}) => ({ project_id: id, project_name: id, project_kind: kind,
  project_status: 'BUILDING', updated_at: '2026-10-01T00:00:00Z', ...extra });

test('the selected acceptance project stays outside the business list and its count', () => {
  const api = lineage(), groups = api.groupProjects([row('acceptance', 'ACCEPTANCE_TEST')]);
  assert.equal(groups[0].current.project_id, 'acceptance');
  for (const [kind, count] of [['BUSINESS', 0], ['ACCEPTANCE_TEST', 1], ['ALL', 1]]) {
    const selection = api.selectProjectGroups(groups, { kind });
    assert.equal(selection.groups.length, count);
    assert.equal(selection.recordCount, count);
  }
  assert.equal(groups.length, 1, 'filtering never removes the detail project or mutates source groups');
});

test('search, type and status intersect and counts use active revisions of the returned groups', () => {
  const api = lineage(), groups = api.groupProjects([
    row('business', 'BUSINESS', { project_name: 'shared query', project_status: 'PUBLISHED' }),
    row('business-revision', 'BUSINESS', { parent_project_id: 'business', project_status: 'QA_FAILED' }),
    row('archived-revision', 'BUSINESS', { parent_project_id: 'business', project_status: 'ARCHIVED' }),
    row('test', 'ACCEPTANCE_TEST', { project_name: 'shared query', project_status: 'PUBLISHED' }),
    row('old', 'BUSINESS', { project_status: 'ARCHIVED' }),
  ]);
  const matchesSearch = group => group.members.some(member => member.project_name.includes('shared'));
  const business = api.selectProjectGroups(groups, { kind: 'BUSINESS', matchesSearch });
  assert.equal(business.groups.length, 1);
  assert.equal(business.groups[0].rootProjectId, 'business');
  assert.equal(business.recordCount, 2);
  const waiting = api.selectProjectGroups(groups, { kind: 'ALL', status: 'WAITING', matchesSearch });
  assert.equal(waiting.groups.length, 1);
  assert.equal(waiting.recordCount, 2);
  const published = api.selectProjectGroups(groups, { kind: 'BUSINESS', status: 'PUBLISHED', matchesSearch });
  assert.equal(published.groups.length, 0);
  assert.equal(published.recordCount, 0);
  const all = api.selectProjectGroups(groups, { kind: 'ALL', matchesSearch });
  assert.equal(all.groups.length, 2);
  assert.equal(all.recordCount, 3);
  const absent = api.selectProjectGroups(groups, { kind: 'ACCEPTANCE_TEST', matchesSearch: () => false });
  assert.equal(absent.groups.length, 0);
  assert.equal(absent.recordCount, 0);
});
